"""
Feed Finlight API v2 — Financial news + sentiment per event_driven strategy.

Finlight fornisce notizie finanziarie in tempo reale con:
- Sentiment analysis (positive/negative/neutral + confidence 0-1)
- Entity extraction (companies, tickers)
- Query avanzate con boolean logic (ticker, source, sentiment)
- Sub-200ms response time

Finlight API v2:
- Endpoint: POST https://api.finlight.me/v2/articles
- Auth: X-API-KEY header
- Response: {"articles": [{title, summary, sentiment, confidence, ...}]}

Uso nel bot:
- event_driven: rileva notizie correlate ai mercati Polymarket,
  usa il sentiment delle news per confermare/smentire l'edge.
  Es: mercato FOMC → cerca news su "fed rate decision" → sentiment positivo
  → aumenta confidenza per la posizione corrispondente.
"""

import logging
import os
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.finlight.me/v2"

# Cache notizie per 3 minuti — le news cambiano ma non ogni secondo
CACHE_TTL = 180

# Mappatura: tipo evento → query Finlight per cercare notizie correlate
EVENT_QUERIES: dict[str, list[str]] = {
    "macro": [
        "federal reserve OR FOMC OR interest rate decision",
        "CPI inflation data OR consumer price index",
        "unemployment OR nonfarm payrolls OR jobs report",
        "GDP growth OR recession OR treasury yield",
        "tariff OR trade war OR debt ceiling",
    ],
    "crypto_regulatory": [
        "bitcoin ETF OR ethereum ETF OR crypto ETF approval",
        "SEC crypto regulation OR CFTC crypto",
        "stablecoin regulation OR CBDC",
        "binance OR coinbase regulation lawsuit",
    ],
    "political": [
        "US election OR presidential election",
        "congress vote OR senate vote",
        "supreme court decision OR ruling",
        "impeachment OR political crisis",
    ],
    "tech": [
        "NVIDIA earnings OR Apple earnings OR tech earnings",
        "AI regulation OR artificial intelligence",
        "tech IPO OR acquisition OR antitrust",
        "semiconductor chip shortage OR supply chain",
    ],
    "geopolitical": [
        "Ukraine Russia war OR ceasefire",
        "China Taiwan tensions OR conflict",
        "Iran Israel conflict OR Middle East",
        "OPEC oil production OR oil price",
        "NATO defense OR sanctions",
    ],
}


@dataclass
class NewsArticle:
    """Singolo articolo di notizia da Finlight."""
    title: str = ""
    summary: str = ""
    source: str = ""
    url: str = ""
    sentiment: str = "neutral"      # "positive", "negative", "neutral"
    confidence: float = 0.0         # 0.0 - 1.0 (confidenza del sentiment)
    published_at: str = ""
    tickers: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)

    @property
    def sentiment_score(self) -> float:
        """
        Segnale numerico: -1.0 (negativo) a +1.0 (positivo).
        Pesato per la confidenza del modello.
        """
        if self.sentiment == "positive":
            return self.confidence
        elif self.sentiment == "negative":
            return -self.confidence
        return 0.0


@dataclass
class NewsSentiment:
    """Sentiment aggregato delle notizie per un tipo di evento."""
    event_type: str
    articles: list[NewsArticle] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.fetched_at) < CACHE_TTL if self.fetched_at > 0 else False

    @property
    def n_articles(self) -> int:
        return len(self.articles)

    @property
    def avg_sentiment(self) -> float:
        """
        Sentiment medio pesato per confidenza: -1.0 a +1.0.
        """
        if not self.articles:
            return 0.0
        total_weight = sum(a.confidence for a in self.articles if a.confidence > 0)
        if total_weight == 0:
            return 0.0
        weighted_sum = sum(a.sentiment_score * a.confidence for a in self.articles)
        return weighted_sum / total_weight

    @property
    def sentiment_label(self) -> str:
        """Label del sentiment aggregato."""
        avg = self.avg_sentiment
        if avg > 0.3:
            return "BULLISH"
        elif avg > 0.1:
            return "MILD_BULL"
        elif avg < -0.3:
            return "BEARISH"
        elif avg < -0.1:
            return "MILD_BEAR"
        return "NEUTRAL"

    @property
    def news_volume(self) -> str:
        """Volume di notizie: alto = evento significativo."""
        n = self.n_articles
        if n >= 10:
            return "HIGH"
        elif n >= 5:
            return "MODERATE"
        elif n >= 1:
            return "LOW"
        return "NONE"

    @property
    def positive_ratio(self) -> float:
        """% di articoli con sentiment positivo."""
        if not self.articles:
            return 0.5
        pos = sum(1 for a in self.articles if a.sentiment == "positive")
        return pos / len(self.articles)

    @property
    def high_confidence_sentiment(self) -> float:
        """
        Sentiment solo degli articoli ad alta confidenza (>0.7).
        Piu' affidabile del sentiment medio.
        """
        hc = [a for a in self.articles if a.confidence > 0.7]
        if not hc:
            return 0.0
        return sum(a.sentiment_score for a in hc) / len(hc)


@dataclass
class FinlightFeed:
    """
    Client per Finlight API v2 — financial news con sentiment.

    Fetcha notizie correlate a eventi per la strategia event_driven.
    Degrada gracefully se API key manca o API irraggiungibile.
    """
    _api_key: str = ""
    _cache: dict[str, NewsSentiment] = field(default_factory=dict)
    _session: requests.Session | None = None
    _available: bool | None = None

    def __post_init__(self):
        self._api_key = os.environ.get("FINLIGHT_API_KEY", "")
        if not self._api_key:
            logger.info("[FINLIGHT] Nessuna API key — news sentiment disabilitato")
            self._available = False
            return

        self._session = requests.Session()
        self._session.headers.update({
            "X-API-KEY": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "PolymarketBot/3.9",
        })

        logger.info(
            f"[FINLIGHT] Feed inizializzato — "
            f"{len(EVENT_QUERIES)} categorie evento"
        )

    # ── Accesso dati ─────────────────────────────────────────────

    def get_event_sentiment(self, event_type: str) -> NewsSentiment:
        """
        Ottieni il sentiment delle notizie per un tipo di evento.
        Cerca notizie correlate e aggrega il sentiment.
        """
        if event_type not in EVENT_QUERIES:
            return NewsSentiment(event_type=event_type)

        cached = self._cache.get(event_type)
        if cached and cached.is_fresh:
            return cached

        self._fetch_event_news(event_type)
        return self._cache.get(event_type, NewsSentiment(event_type=event_type))

    def get_market_sentiment(self, question: str, event_type: str) -> NewsSentiment:
        """
        Cerca notizie specificamente correlate a un mercato Polymarket.
        Usa la domanda del mercato come query + le keyword dell'evento.
        """
        # Cache key basata su question hash + event_type
        cache_key = f"mkt_{hash(question) % 10000}_{event_type}"

        cached = self._cache.get(cache_key)
        if cached and cached.is_fresh:
            return cached

        # Estrai termini chiave dalla domanda
        query = self._question_to_query(question)
        if not query:
            return NewsSentiment(event_type=event_type)

        articles = self._fetch_articles(query, page_size=5)
        ns = NewsSentiment(
            event_type=event_type,
            articles=articles,
            fetched_at=time.time(),
        )
        self._cache[cache_key] = ns
        return ns

    def scan_all_categories(self) -> dict[str, NewsSentiment]:
        """
        Scansiona TUTTE le categorie di eventi in un colpo solo.
        Usato per rilevare breaking news: categorie con volume alto
        e sentiment forte indicano eventi in corso.

        Returns: {event_type: NewsSentiment} per categorie con articoli.
        """
        results = {}
        for etype in EVENT_QUERIES:
            ns = self.get_event_sentiment(etype)
            if ns.n_articles > 0:
                results[etype] = ns
        return results

    def detect_breaking_news(self, min_articles: int = 5, min_sentiment: float = 0.3) -> list[tuple[str, NewsSentiment]]:
        """
        Rileva breaking news: categorie con molti articoli recenti
        e sentiment forte (positivo o negativo).

        Questo è il segnale più forte per event-driven trading:
        molte news + sentiment unidirezionale = evento in corso
        che il mercato potrebbe non aver ancora assorbito.

        Returns: [(event_type, NewsSentiment)] ordinato per |sentiment| * n_articles
        """
        breaking = []
        all_ns = self.scan_all_categories()

        for etype, ns in all_ns.items():
            if ns.n_articles >= min_articles and abs(ns.avg_sentiment) >= min_sentiment:
                breaking.append((etype, ns))

        # Ordina per "news_strength": |sentiment| * n_articles
        breaking.sort(
            key=lambda x: abs(x[1].avg_sentiment) * x[1].n_articles,
            reverse=True,
        )

        if breaking:
            logger.info(
                f"[FINLIGHT] BREAKING NEWS: {len(breaking)} categorie — "
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

        0.0 = nessuna news o news vecchie/neutrali
        0.5 = news moderate con sentiment leggero
        1.0 = molte news fresche con sentiment fortissimo
        """
        ns = self.get_event_sentiment(event_type)
        if not ns.is_fresh or ns.n_articles == 0:
            return 0.0

        # Volume score: 1 art=0.1, 5 art=0.5, 10+=1.0
        vol_score = min(ns.n_articles / 10.0, 1.0)

        # Sentiment score: |avg_sentiment| normalizzato 0-1
        sent_score = min(abs(ns.avg_sentiment) / 0.6, 1.0)

        # High-confidence boost: se gli articoli ad alta confidenza concordano
        hc = ns.high_confidence_sentiment
        hc_boost = 0.0
        if abs(hc) > 0.5 and ns.n_articles >= 3:
            hc_boost = 0.15

        strength = (vol_score * 0.4 + sent_score * 0.5 + hc_boost)
        return min(strength, 1.0)

    def sentiment_summary(self) -> str:
        """Stringa riassuntiva per il log."""
        parts = []
        for etype in EVENT_QUERIES:
            ns = self._cache.get(etype)
            if ns and ns.is_fresh and ns.articles:
                parts.append(
                    f"{etype}: {ns.sentiment_label} "
                    f"({ns.n_articles} art, avg={ns.avg_sentiment:+.2f})"
                )
        return " | ".join(parts) if parts else "News: --"

    # ── Fetch da API ─────────────────────────────────────────────

    def _fetch_event_news(self, event_type: str):
        """Fetcha notizie per tutte le query di un tipo di evento."""
        if self._available is False or not self._session:
            return

        queries = EVENT_QUERIES.get(event_type, [])
        all_articles: list[NewsArticle] = []

        for query in queries:
            articles = self._fetch_articles(query, page_size=5)
            all_articles.extend(articles)

            if articles and self._available is None:
                self._available = True
                logger.info("[FINLIGHT] API connessa — notizie disponibili")

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
                f"[FINLIGHT] {event_type}: {len(unique_articles)} articoli, "
                f"sentiment={ns.sentiment_label} ({ns.avg_sentiment:+.2f})"
            )

    def _fetch_articles(self, query: str, page_size: int = 10) -> list[NewsArticle]:
        """
        Chiama POST /v2/articles con una query.

        Body: {"query": "...", "language": "en", "pageSize": N, "order": "DESC"}
        Response: {"articles": [{title, summary, sentiment, confidence, ...}]}
        """
        if not self._session or self._available is False:
            return []

        body = {
            "query": query,
            "language": "en",
            "pageSize": page_size,
            "order": "DESC",
            "includeEntities": True,
        }

        try:
            resp = self._session.post(
                f"{BASE_URL}/articles",
                json=body,
                timeout=10,
            )

            if resp.status_code == 200:
                data = resp.json()
                return self._parse_articles(data)

            elif resp.status_code == 401:
                logger.warning("[FINLIGHT] API key non valida (401)")
                self._available = False
                return []

            elif resp.status_code == 429:
                logger.warning("[FINLIGHT] Rate limit raggiunto (429)")
                return []

            else:
                logger.debug(f"[FINLIGHT] HTTP {resp.status_code} per query '{query[:30]}'")
                return []

        except requests.Timeout:
            logger.debug(f"[FINLIGHT] Timeout per query '{query[:30]}'")
            return []
        except requests.RequestException as e:
            logger.debug(f"[FINLIGHT] Errore: {e}")
            return []

    # ── Parsing ──────────────────────────────────────────────────

    def _parse_articles(self, data: dict) -> list[NewsArticle]:
        """
        Parsa la risposta di /v2/articles.

        Formati possibili:
        - {"articles": [...]}
        - {"data": [...]}
        - {"results": [...]}
        - [...]
        """
        items = []
        if isinstance(data, list):
            items = data
        elif "articles" in data:
            items = data["articles"]
        elif "data" in data:
            items = data["data"]
        elif "results" in data:
            items = data["results"]

        if not isinstance(items, list):
            return []

        articles = []
        for item in items:
            if not isinstance(item, dict):
                continue

            # Estrai tickers
            tickers = item.get("tickers", [])
            if not tickers:
                # Prova da companies
                companies_data = item.get("companies", [])
                if isinstance(companies_data, list):
                    for c in companies_data:
                        if isinstance(c, dict):
                            t = c.get("ticker") or c.get("symbol")
                            if t:
                                tickers.append(t)

            # Estrai nomi companies
            company_names = []
            companies_data = item.get("companies", [])
            if isinstance(companies_data, list):
                for c in companies_data:
                    if isinstance(c, dict):
                        name = c.get("name") or c.get("company")
                        if name:
                            company_names.append(name)
                    elif isinstance(c, str):
                        company_names.append(c)

            articles.append(NewsArticle(
                title=item.get("title", ""),
                summary=item.get("summary", item.get("description", "")),
                source=item.get("source", item.get("publisher", "")),
                url=item.get("link", item.get("url", "")),
                sentiment=self._normalize_sentiment(item.get("sentiment", "neutral")),
                confidence=self._safe_float(item, "confidence", 0.5),
                published_at=item.get("publishDate", item.get("publishedAt", "")),
                tickers=tickers if isinstance(tickers, list) else [],
                companies=company_names,
            ))

        return articles

    def _question_to_query(self, question: str) -> str:
        """
        Converte una domanda Polymarket in una query Finlight.
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

    @staticmethod
    def _normalize_sentiment(raw: str | None) -> str:
        """Normalizza il sentiment a positive/negative/neutral."""
        if not raw:
            return "neutral"
        r = str(raw).lower().strip()
        if r in ("positive", "pos", "bullish", "up"):
            return "positive"
        elif r in ("negative", "neg", "bearish", "down"):
            return "negative"
        return "neutral"

    @staticmethod
    def _safe_float(d: dict, key: str, default: float = 0.0) -> float:
        val = d.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default
