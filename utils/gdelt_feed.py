"""
Feed GDELT API v2 — Global news + tone per event_driven strategy.

GDELT (Global Database of Events, Language and Tone) monitora media globali
con aggiornamenti ogni 15 minuti, copertura 265+ lingue e sentiment/tone integrato.

Complementa Finlight: GDELT ha copertura superiore su eventi politici e geopolitici
(le categorie piu' profittevoli secondo Becker Dataset: politics +$18.6M PnL),
mentre Finlight e' piu' preciso per news finanziarie/crypto.

GDELT DOC 2.0 API:
- Endpoint: GET https://api.gdeltproject.org/api/v2/doc/doc
- Auth: Nessuna (gratuito)
- Params: query, mode=artlist, format=json, sourcelang=english, sort=datedesc
- Response: {"articles": [{url, title, seendate, domain, language, tone, ...}]}

Uso nel bot:
- event_driven: fonte complementare a Finlight per breaking news detection.
  Merge multi-fonte: prende la fonte con segnale piu' forte per categoria.
  Se entrambe concordano → boost 10% alla news_strength.
"""

import logging
import time

import requests
from dataclasses import dataclass, field

from utils.finlight_feed import NewsArticle, NewsSentiment, CACHE_TTL

logger = logging.getLogger(__name__)

GDELT_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# Query per categoria (stesse 5 categorie di event_driven)
GDELT_QUERIES: dict[str, list[str]] = {
    "macro": [
        "federal reserve OR FOMC OR interest rate",
        "inflation OR CPI OR consumer price index",
        "unemployment OR nonfarm payrolls OR jobs report",
        "GDP growth OR recession OR treasury yield",
        "tariff OR trade war OR debt ceiling",
    ],
    "crypto_regulatory": [
        "bitcoin ETF OR crypto regulation",
        "SEC cryptocurrency OR CFTC crypto",
        "stablecoin regulation OR CBDC",
        "binance OR coinbase regulation",
    ],
    "political": [
        "US election OR presidential election",
        "congress vote OR senate vote OR legislation",
        "supreme court decision OR ruling",
        "impeachment OR political crisis",
        "executive order OR presidential approval",
    ],
    "tech": [
        "NVIDIA earnings OR Apple earnings OR tech earnings",
        "AI regulation OR artificial intelligence policy",
        "tech IPO OR acquisition OR antitrust",
        "semiconductor shortage OR chip supply",
    ],
    "geopolitical": [
        "Ukraine Russia war OR ceasefire",
        "China Taiwan tensions OR strait",
        "Iran Israel conflict OR Middle East",
        "OPEC oil production OR oil price",
        "NATO defense OR sanctions OR embargo",
    ],
}

# Discount vs Finlight: GDELT tone e' meno preciso di NLP fine-tuned
GDELT_STRENGTH_DISCOUNT = 0.90  # 10% discount

# Circuit breaker: disabilita dopo N errori consecutivi, retry dopo cooldown
MAX_CONSECUTIVE_ERRORS = 5
CIRCUIT_BREAKER_COOLDOWN = 300  # 5 minuti

# v9.2.2: Rate limit GDELT — intervallo conservativo per evitare ban IP
GDELT_MIN_REQUEST_INTERVAL = 10.0  # secondi tra richieste (era 5.5, aumentato per stabilita')


@dataclass
class GDELTFeed:
    """
    Client per GDELT DOC 2.0 API — global news con tone analysis.

    Produce gli stessi tipi (NewsArticle, NewsSentiment) di FinlightFeed
    per permettere merge trasparente in event_driven.
    Degrada gracefully se GDELT e' irraggiungibile.
    """
    _cache: dict[str, NewsSentiment] = field(default_factory=dict)
    _available: bool | None = None  # None=non testato, True=ok, False=down
    _consecutive_errors: int = 0
    _consecutive_rate_limits: int = 0  # v9.2.2: tracking 200 text-body rate limit
    _circuit_breaker_at: float = 0.0  # timestamp quando e' scattato
    _circuit_breaker_trips: int = 0  # v9.2.2: contatore trip per cooldown escalante
    _last_request_at: float = 0.0  # v9.2.1: rate limit tracking

    def __post_init__(self):
        logger.info(
            f"[GDELT] Feed inizializzato — "
            f"{len(GDELT_QUERIES)} categorie evento"
        )

    @property
    def is_healthy(self) -> bool:
        """v9.2.2: True se il feed e' disponibile per query costose (per-mercato)."""
        if self._available is False:
            return False  # circuit breaker attivo
        if self._consecutive_errors >= 2:
            return False  # troppi errori recenti
        if self._consecutive_rate_limits >= 2:
            return False  # IP rate limited (200 text-body)
        return True

    # ── Accesso dati ─────────────────────────────────────────────

    def get_event_sentiment(self, event_type: str) -> NewsSentiment:
        """
        Ottieni il sentiment delle notizie per un tipo di evento.
        Cerca notizie correlate su GDELT e aggrega il tone.
        """
        if event_type not in GDELT_QUERIES:
            return NewsSentiment(event_type=event_type)

        cached = self._cache.get(event_type)
        if cached and cached.is_fresh:
            return cached

        self._fetch_event_news(event_type)
        return self._cache.get(event_type, NewsSentiment(event_type=event_type))

    def get_market_sentiment(self, question: str, event_type: str) -> NewsSentiment:
        """
        Cerca notizie specificamente correlate a un mercato Polymarket.
        Usa la domanda del mercato come query.
        """
        cache_key = f"mkt_{hash(question) % 10000}_{event_type}"

        cached = self._cache.get(cache_key)
        if cached and cached.is_fresh:
            return cached

        query = self._question_to_query(question)
        if not query:
            return NewsSentiment(event_type=event_type)

        articles = self._fetch_articles(query, max_records=10)
        ns = NewsSentiment(
            event_type=event_type,
            articles=articles,
            fetched_at=time.time(),
        )
        self._cache[cache_key] = ns
        return ns

    def detect_breaking_events(
        self, min_articles: int = 5, min_sentiment: float = 0.25
    ) -> list[tuple[str, NewsSentiment]]:
        """
        Rileva breaking news: categorie con molti articoli recenti
        e tone forte (positivo o negativo).

        Returns: [(event_type, NewsSentiment)] ordinato per |sentiment| * n_articles
        """
        breaking = []

        for etype in GDELT_QUERIES:
            ns = self.get_event_sentiment(etype)
            if ns.n_articles >= min_articles and abs(ns.avg_sentiment) >= min_sentiment:
                breaking.append((etype, ns))

        breaking.sort(
            key=lambda x: abs(x[1].avg_sentiment) * x[1].n_articles,
            reverse=True,
        )

        if breaking:
            logger.info(
                f"[GDELT] BREAKING: {len(breaking)} categorie — "
                + " | ".join(
                    f"{et}: {ns.sentiment_label}({ns.n_articles}art, {ns.avg_sentiment:+.2f})"
                    for et, ns in breaking
                )
            )

        return breaking

    def get_news_strength(self, event_type: str) -> float:
        """
        Calcola la "forza" delle news per un evento: 0.0 a 1.0.
        Combina volume di articoli, sentiment e freschezza.
        Applica discount 10% vs Finlight (GDELT tone meno preciso).
        """
        ns = self.get_event_sentiment(event_type)
        if not ns.is_fresh or ns.n_articles == 0:
            return 0.0

        # Volume score: 1 art=0.1, 5 art=0.5, 10+=1.0
        vol_score = min(ns.n_articles / 10.0, 1.0)

        # Sentiment score: |avg_sentiment| normalizzato 0-1
        sent_score = min(abs(ns.avg_sentiment) / 0.6, 1.0)

        # High-confidence boost
        hc = ns.high_confidence_sentiment
        hc_boost = 0.0
        if abs(hc) > 0.5 and ns.n_articles >= 3:
            hc_boost = 0.15

        strength = (vol_score * 0.4 + sent_score * 0.5 + hc_boost)
        # Discount: GDELT tone e' meno preciso di NLP fine-tuned
        strength *= GDELT_STRENGTH_DISCOUNT
        return min(strength, 1.0)

    # ── Fetch da API ─────────────────────────────────────────────

    def _fetch_event_news(self, event_type: str):
        """Fetcha notizie per tutte le query di un tipo di evento."""
        if not self._check_circuit_breaker():
            return

        queries = GDELT_QUERIES.get(event_type, [])
        # v9.2.2: Query singola (la prima, piu' rilevante) per categoria.
        # Le query combinate con OR causavano timeout sistematici su GDELT
        # perche' la ricerca full-text su miliardi di articoli e' troppo lenta
        # con boolean OR complessi. Una query semplice risponde in 2-5s.
        query = queries[0] if queries else ""
        if not query:
            return
        all_articles = self._fetch_articles(query, max_records=10, timespan="4h")

        # Deduplica per URL
        seen_urls: set[str] = set()
        unique_articles: list[NewsArticle] = []
        for a in all_articles:
            if a.url and a.url not in seen_urls:
                seen_urls.add(a.url)
                unique_articles.append(a)
            elif not a.url:
                unique_articles.append(a)

        ns = NewsSentiment(
            event_type=event_type,
            articles=unique_articles,
            fetched_at=time.time(),
        )
        self._cache[event_type] = ns

        if unique_articles:
            logger.debug(
                f"[GDELT] {event_type}: {len(unique_articles)} articoli, "
                f"sentiment={ns.sentiment_label} ({ns.avg_sentiment:+.2f})"
            )

    def _fetch_articles(
        self, query: str, max_records: int = 10, timespan: str = "4h"
    ) -> list[NewsArticle]:
        """
        GET a GDELT DOC 2.0 API.

        Params: query, mode=artlist, format=json, sourcelang=english,
                sort=datedesc, maxrecords, timespan
        Response: {"articles": [{url, title, seendate, domain, language, tone, ...}]}
        """
        if not self._check_circuit_breaker():
            return []

        # v9.2.1: Rate limit — GDELT richiede minimo 5s tra richieste
        now = time.time()
        elapsed = now - self._last_request_at
        if elapsed < GDELT_MIN_REQUEST_INTERVAL:
            wait = GDELT_MIN_REQUEST_INTERVAL - elapsed
            time.sleep(wait)
        self._last_request_at = time.time()

        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "sourcelang": "english",
            "sort": "datedesc",
            "maxrecords": max_records,
            "timespan": timespan,
        }

        try:
            resp = requests.get(
                GDELT_BASE_URL,
                params=params,
                timeout=30,  # v9.2.2: 30s per query semplici (era 20s per combinate)
            )

            if resp.status_code == 200:
                # v9.2.1: GDELT a volte ritorna 200 con body di rate limit
                text = resp.text.strip()
                if text.startswith("Please limit") or not text.startswith("{"):
                    logger.warning("[GDELT] Rate limit (200 con body testo) — attesa extra")
                    self._last_request_at = time.time() + 10  # v9.2.2: attesa piu' lunga
                    self._consecutive_rate_limits += 1  # v9.2.2: tracking per is_healthy
                    return []

                # Reset errori consecutivi e rate limit
                self._consecutive_errors = 0
                self._consecutive_rate_limits = 0
                if self._available is None:
                    self._available = True
                    logger.info("[GDELT] API connessa — notizie disponibili")

                data = resp.json()
                return self._parse_articles(data)

            elif resp.status_code == 429:
                logger.warning(f"[GDELT] Rate limit 429 — attesa prima del prossimo tentativo")
                self._last_request_at = time.time() + 5  # forza attesa extra
                self._register_error()
                return []
            else:
                logger.warning(f"[GDELT] HTTP {resp.status_code} per query '{query[:40]}'")
                self._register_error()
                return []

        except requests.Timeout:
            logger.warning(f"[GDELT] Timeout per query '{query[:40]}'")
            self._register_error()
            return []
        except requests.RequestException as e:
            logger.warning(f"[GDELT] Errore: {e}")
            self._register_error()
            return []
        except ValueError:
            # JSON decode error — GDELT a volte ritorna testo invece di JSON
            logger.warning(f"[GDELT] Risposta non-JSON per query '{query[:40]}'")
            self._register_error()
            return []

    def _check_circuit_breaker(self) -> bool:
        """Controlla se il circuit breaker e' scattato. Se il cooldown e' passato, resetta."""
        if self._available is not False:
            return True  # feed attivo
        # v9.2.2: Cooldown escalante basato su quante volte il breaker e' scattato
        cooldowns = [60, 180, 300, 600]  # 1min, 3min, 5min, 10min
        idx = min(self._circuit_breaker_trips - 1, len(cooldowns) - 1)
        cooldown = cooldowns[max(idx, 0)]
        elapsed = time.time() - self._circuit_breaker_at
        if elapsed >= cooldown:
            logger.info(
                f"[GDELT] Circuit breaker reset dopo {elapsed:.0f}s "
                f"(trip #{self._circuit_breaker_trips}) — retry"
            )
            self._available = None
            self._consecutive_errors = 0
            return True  # riprova
        return False  # ancora in cooldown

    def _register_error(self):
        """Circuit breaker: disabilita dopo MAX_CONSECUTIVE_ERRORS errori."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            self._circuit_breaker_at = time.time()
            self._circuit_breaker_trips += 1
            cooldowns = [60, 180, 300, 600]
            idx = min(self._circuit_breaker_trips - 1, len(cooldowns) - 1)
            cooldown = cooldowns[max(idx, 0)]
            logger.warning(
                f"[GDELT] {self._consecutive_errors} errori consecutivi — "
                f"feed disabilitato (circuit breaker trip #{self._circuit_breaker_trips}, "
                f"retry tra {cooldown}s)"
            )
            self._available = False

    # ── Parsing ──────────────────────────────────────────────────

    def _parse_articles(self, data: dict) -> list[NewsArticle]:
        """
        Parsa la risposta di GDELT DOC 2.0 API.

        Formato: {"articles": [{url, title, seendate, domain, language, tone, ...}]}
        Il campo tone e' una stringa CSV: "tone,pos,neg,polarity,arf,srf,wc"
        """
        items = []
        if isinstance(data, dict):
            items = data.get("articles", [])
        elif isinstance(data, list):
            items = data

        if not isinstance(items, list):
            return []

        articles = []
        for item in items:
            if not isinstance(item, dict):
                continue

            # Parse tone CSV
            tone_str = item.get("tone", "")
            sentiment_label, sentiment_score, confidence = self._parse_tone(tone_str)

            articles.append(NewsArticle(
                title=item.get("title", ""),
                summary="",  # GDELT non fornisce summary
                source=item.get("domain", ""),
                url=item.get("url", ""),
                sentiment=sentiment_label,
                confidence=confidence,
                published_at=item.get("seendate", ""),
                tickers=[],
                companies=[],
            ))

        return articles

    @staticmethod
    def _parse_tone(tone_str: str) -> tuple[str, float, float]:
        """
        Converte tone GDELT (CSV) in (sentiment_label, normalized_score, confidence).

        Formato tone: "tone,positive_score,negative_score,polarity,arf,srf,word_count"
        - tone: average tone of document (-100 to +100, tipicamente -15 a +15)
        - Normalizziamo: tone / 10.0 per range ~-1.0/+1.0

        Confidence cappata a 0.85 (GDELT meno preciso di NLP fine-tuned).
        Threshold: |normalized| > 0.15 per positive/negative, altrimenti neutral.
        """
        if not tone_str:
            return "neutral", 0.0, 0.0

        try:
            parts = tone_str.split(",")
            raw_tone = float(parts[0])
        except (ValueError, IndexError):
            return "neutral", 0.0, 0.0

        # Normalizza in range -1.0/+1.0
        normalized = max(-1.0, min(1.0, raw_tone / 10.0))

        # Confidence basata sulla magnitudine del tone
        # Tone piu' forte = piu' confidenza (ma cap a 0.85)
        confidence = min(abs(normalized) * 1.2, 0.85)

        # Threshold per label
        if normalized > 0.15:
            label = "positive"
        elif normalized < -0.15:
            label = "negative"
        else:
            label = "neutral"

        return label, normalized, confidence

    def _question_to_query(self, question: str) -> str:
        """
        Converte una domanda Polymarket in una query GDELT.
        Rimuove parole generiche, mantiene i termini chiave.
        """
        stop_words = {
            "will", "the", "be", "by", "on", "in", "at", "to", "of",
            "a", "an", "is", "or", "and", "for", "this", "that", "it",
            "yes", "no", "before", "after", "end", "day",
        }
        words = question.lower().split()
        key_words = [w for w in words if w not in stop_words and len(w) > 2]

        if len(key_words) < 2:
            return ""

        # Prendi le prime 5 parole chiave
        return " ".join(key_words[:5])
