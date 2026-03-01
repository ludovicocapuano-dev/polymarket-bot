"""
Feed Twitter/X — Breaking news via twscrape per event_driven strategy.

Twitter/X è la fonte più veloce per breaking political/crypto news.
Complementa Finlight, GDELT e Glint come 4a fonte nel merge multi-sorgente.

Libreria: twscrape (async, free, account-based auth).
Sentiment: VADER (già nel progetto). Confidence cappata a 0.80, strength discount 15%.

Senza credenziali (TWITTER_ACCOUNTS env var) → feed disabilitato, noop.
"""

import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Configurazione ────────────────────────────────────────────

CACHE_TTL = 180  # 3 minuti, uguale a Finlight/GDELT
RATE_LIMIT_INTERVAL = 180  # 1 search per categoria ogni 180s

# Discount vs Finlight: VADER meno preciso di NLP fine-tuned
TWITTER_STRENGTH_DISCOUNT = 0.85  # 15% discount

# Confidence cap: VADER su tweet brevi è meno affidabile
TWITTER_CONFIDENCE_CAP = 0.80

# Circuit breaker
MAX_CONSECUTIVE_ERRORS = 5
CIRCUIT_BREAKER_COOLDOWNS = [60, 180, 300, 600]  # escalante

# Engagement: soglia per log2 scaling
MAX_ENGAGEMENT_FOR_SCALE = 100_000  # log2(100K) ≈ 16.6

# ── Import condizionale ──────────────────────────────────────

try:
    import twscrape
    import asyncio
    _HAS_TWSCRAPE = True
except ImportError:
    _HAS_TWSCRAPE = False

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _HAS_VADER = True
except ImportError:
    _HAS_VADER = False

# Tipi condivisi
from utils.finlight_feed import NewsArticle, NewsSentiment

# ── Query per categoria (stesse 5 categorie di event_driven) ─

TWITTER_QUERIES: dict[str, list[str]] = {
    "political": [
        "US election OR presidential race",
        "congress vote OR senate bill",
        "supreme court ruling OR SCOTUS",
        "executive order OR White House",
        "impeachment OR political crisis",
    ],
    "crypto_regulatory": [
        "bitcoin ETF OR crypto ETF approval",
        "SEC crypto OR CFTC regulation",
        "stablecoin bill OR CBDC",
        "binance OR coinbase lawsuit",
        "crypto ban OR regulation",
    ],
    "geopolitical": [
        "Ukraine Russia war OR ceasefire",
        "China Taiwan conflict OR tensions",
        "Iran Israel OR Middle East crisis",
        "OPEC oil production cut",
        "NATO defense OR sanctions",
    ],
    "macro": [
        "federal reserve OR FOMC decision",
        "CPI inflation OR consumer prices",
        "nonfarm payrolls OR jobs report",
        "GDP growth OR recession fears",
        "tariff trade war OR debt ceiling",
    ],
    "tech": [
        "NVIDIA earnings OR Apple earnings",
        "AI regulation OR artificial intelligence policy",
        "tech IPO OR antitrust",
        "semiconductor chip shortage",
        "tech layoffs OR hiring freeze",
    ],
}


@dataclass
class TwitterFeed:
    """
    Client Twitter/X via twscrape — breaking news con VADER sentiment.

    Produce gli stessi tipi (NewsArticle, NewsSentiment) di FinlightFeed/GDELTFeed
    per permettere merge trasparente in event_driven.
    Degrada gracefully se twscrape/VADER mancano o credenziali assenti.
    """

    _available: bool | None = None  # None=non testato, True=ok, False=disabilitato
    _cache: dict[str, NewsSentiment] = field(default_factory=dict)
    _rate_limit_at: dict[str, float] = field(default_factory=dict)  # categoria → last fetch
    _consecutive_errors: int = 0
    _circuit_breaker_at: float = 0.0
    _circuit_breaker_trips: int = 0
    _accounts_json: str = ""
    _logged_in: bool = False
    _api: object = None  # twscrape.API
    _vader: object = None  # SentimentIntensityAnalyzer
    _executor: ThreadPoolExecutor | None = None

    def __post_init__(self):
        # Check dipendenze
        if not _HAS_TWSCRAPE:
            logger.info("[TWITTER] twscrape non installato — feed disabilitato")
            self._available = False
            return
        if not _HAS_VADER:
            logger.info("[TWITTER] vaderSentiment non installato — feed disabilitato")
            self._available = False
            return

        # Credenziali da env var
        self._accounts_json = os.environ.get("TWITTER_ACCOUNTS", "")
        if not self._accounts_json:
            logger.info("[TWITTER] Nessuna credenziale (TWITTER_ACCOUNTS) — feed disabilitato")
            self._available = False
            return

        # Valida JSON
        try:
            accounts = json.loads(self._accounts_json)
            if not isinstance(accounts, list) or len(accounts) == 0:
                raise ValueError("Array vuoto")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[TWITTER] TWITTER_ACCOUNTS JSON invalido: {e} — feed disabilitato")
            self._available = False
            return

        # Init VADER
        self._vader = SentimentIntensityAnalyzer()

        # Init thread pool per async bridge
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="twitter")

        logger.info(
            f"[TWITTER] Feed inizializzato — "
            f"{len(TWITTER_QUERIES)} categorie, {len(accounts)} account"
        )

    # ── Properties ────────────────────────────────────────────

    @property
    def is_healthy(self) -> bool:
        """True se il feed è disponibile per query."""
        if self._available is False:
            return False
        if self._consecutive_errors >= 2:
            return False
        return True

    # ── Interfaccia pubblica (stessa di Finlight/GDELT) ──────

    def get_event_sentiment(self, event_type: str) -> NewsSentiment:
        """
        Ottieni il sentiment delle notizie per un tipo di evento.
        Cerca tweet correlati e aggrega il sentiment VADER.
        """
        if self._available is False:
            return NewsSentiment(event_type=event_type)
        if event_type not in TWITTER_QUERIES:
            return NewsSentiment(event_type=event_type)

        cached = self._cache.get(event_type)
        if cached and cached.is_fresh:
            return cached

        self._fetch_event_tweets(event_type)
        return self._cache.get(event_type, NewsSentiment(event_type=event_type))

    def get_market_sentiment(self, question: str, event_type: str) -> NewsSentiment:
        """
        Cerca tweet specificamente correlati a un mercato Polymarket.
        Usa la domanda del mercato come query.
        """
        if self._available is False:
            return NewsSentiment(event_type=event_type)

        cache_key = f"mkt_{hash(question) % 10000}_{event_type}"
        cached = self._cache.get(cache_key)
        if cached and cached.is_fresh:
            return cached

        query = self._question_to_query(question)
        if not query:
            return NewsSentiment(event_type=event_type)

        articles = self._fetch_tweets(query, max_tweets=15)
        ns = NewsSentiment(
            event_type=event_type,
            articles=articles,
            fetched_at=time.time(),
        )
        self._cache[cache_key] = ns
        return ns

    def get_news_strength(self, event_type: str) -> float:
        """
        Calcola la "forza" delle news per un evento: 0.0 a 1.0.
        Combina volume tweet, sentiment e freschezza.
        Applica discount 15% vs Finlight (VADER meno preciso).
        """
        ns = self.get_event_sentiment(event_type)
        if not ns.is_fresh or ns.n_articles == 0:
            return 0.0

        # Volume score: 1 tweet=0.1, 5=0.5, 10+=1.0
        vol_score = min(ns.n_articles / 10.0, 1.0)

        # Sentiment score: |avg_sentiment| normalizzato 0-1
        sent_score = min(abs(ns.avg_sentiment) / 0.6, 1.0)

        # High-confidence boost
        hc = ns.high_confidence_sentiment
        hc_boost = 0.0
        if abs(hc) > 0.5 and ns.n_articles >= 3:
            hc_boost = 0.15

        strength = vol_score * 0.4 + sent_score * 0.5 + hc_boost
        # Discount: VADER su tweet brevi è meno preciso di NLP fine-tuned
        strength *= TWITTER_STRENGTH_DISCOUNT
        return min(strength, 1.0)

    def detect_breaking_news(
        self, min_articles: int = 3, min_sentiment: float = 0.25
    ) -> list[tuple[str, NewsSentiment]]:
        """
        Rileva breaking news: categorie con molti tweet recenti
        e sentiment forte (positivo o negativo).

        Returns: [(event_type, NewsSentiment)] ordinato per |sentiment| * n_articles
        """
        if self._available is False:
            return []

        breaking = []

        for etype in TWITTER_QUERIES:
            ns = self.get_event_sentiment(etype)
            if ns.n_articles >= min_articles and abs(ns.avg_sentiment) >= min_sentiment:
                breaking.append((etype, ns))

        breaking.sort(
            key=lambda x: abs(x[1].avg_sentiment) * x[1].n_articles,
            reverse=True,
        )

        if breaking:
            logger.info(
                f"[TWITTER] BREAKING: {len(breaking)} categorie — "
                + " | ".join(
                    f"{et}: {ns.sentiment_label}({ns.n_articles}tw, {ns.avg_sentiment:+.2f})"
                    for et, ns in breaking
                )
            )

        return breaking

    # ── Fetch da Twitter ──────────────────────────────────────

    def _fetch_event_tweets(self, event_type: str):
        """Fetcha tweet per la prima query di un tipo di evento."""
        if not self._check_circuit_breaker():
            return

        # Rate limit per categoria
        now = time.time()
        last = self._rate_limit_at.get(event_type, 0.0)
        if now - last < RATE_LIMIT_INTERVAL:
            return

        queries = TWITTER_QUERIES.get(event_type, [])
        query = queries[0] if queries else ""
        if not query:
            return

        articles = self._fetch_tweets(query, max_tweets=20)
        self._rate_limit_at[event_type] = time.time()

        # Deduplica per URL
        seen: set[str] = set()
        unique: list[NewsArticle] = []
        for a in articles:
            if a.url and a.url not in seen:
                seen.add(a.url)
                unique.append(a)
            elif not a.url:
                unique.append(a)

        ns = NewsSentiment(
            event_type=event_type,
            articles=unique,
            fetched_at=time.time(),
        )
        self._cache[event_type] = ns

        if unique:
            logger.debug(
                f"[TWITTER] {event_type}: {len(unique)} tweet, "
                f"sentiment={ns.sentiment_label} ({ns.avg_sentiment:+.2f})"
            )

    def _fetch_tweets(self, query: str, max_tweets: int = 20) -> list[NewsArticle]:
        """
        Cerca tweet via twscrape (async → sync bridge).
        Lazy login al primo fetch.
        """
        if not self._check_circuit_breaker():
            return []

        try:
            # Lazy init API + login
            if self._api is None:
                self._api = twscrape.API()
            if not self._logged_in:
                self._lazy_login()
                if not self._logged_in:
                    return []

            # Async bridge: run twscrape search in thread
            articles = self._run_async_search(query, max_tweets)

            # Reset errori su successo
            self._consecutive_errors = 0
            if self._available is None:
                self._available = True
                logger.info("[TWITTER] API connessa — tweet disponibili")

            return articles

        except Exception as e:
            logger.warning(f"[TWITTER] Errore fetch: {e}")
            self._register_error()
            return []

    def _lazy_login(self):
        """Login account twscrape al primo utilizzo."""
        try:
            accounts = json.loads(self._accounts_json)

            def _do_login():
                loop = asyncio.new_event_loop()
                try:
                    for acc in accounts:
                        loop.run_until_complete(
                            self._api.pool.add_account(
                                acc.get("username", ""),
                                acc.get("password", ""),
                                acc.get("email", ""),
                                acc.get("email_password", acc.get("password", "")),
                            )
                        )
                    loop.run_until_complete(self._api.pool.login_all())
                finally:
                    loop.close()

            self._executor.submit(_do_login).result(timeout=30)
            self._logged_in = True
            logger.info("[TWITTER] Account loggati con successo")

        except Exception as e:
            logger.warning(f"[TWITTER] Login fallito: {e} — feed disabilitato")
            self._available = False

    def _run_async_search(self, query: str, max_tweets: int) -> list[NewsArticle]:
        """Esegue ricerca async twscrape in un thread separato."""

        def _do_search():
            loop = asyncio.new_event_loop()
            try:
                tweets = loop.run_until_complete(
                    self._collect_tweets(query, max_tweets)
                )
                return tweets
            finally:
                loop.close()

        raw_tweets = self._executor.submit(_do_search).result(timeout=30)
        return [self._tweet_to_article(t) for t in raw_tweets]

    async def _collect_tweets(self, query: str, max_tweets: int) -> list:
        """Raccoglie tweet dalla search API di twscrape."""
        tweets = []
        async for tweet in self._api.search(query, limit=max_tweets):
            tweets.append(tweet)
        return tweets

    # ── Parsing tweet → NewsArticle ───────────────────────────

    def _tweet_to_article(self, tweet) -> NewsArticle:
        """
        Converte un tweet twscrape in NewsArticle con VADER sentiment.

        Confidence = vader_weight*0.65 + log2_engagement*0.30 + verified*0.05
        Cappata a TWITTER_CONFIDENCE_CAP (0.80).
        """
        text = tweet.rawContent if hasattr(tweet, "rawContent") else str(tweet)
        username = tweet.user.username if hasattr(tweet, "user") and tweet.user else "unknown"

        # VADER sentiment
        vader_scores = self._vader.polarity_scores(text)
        compound = vader_scores["compound"]  # -1.0 a +1.0

        # Engagement: likes + retweets + replies
        likes = getattr(tweet, "likeCount", 0) or 0
        retweets = getattr(tweet, "retweetCount", 0) or 0
        replies = getattr(tweet, "replyCount", 0) or 0
        engagement = likes + retweets + replies

        # Verified check
        verified = False
        if hasattr(tweet, "user") and tweet.user:
            verified = getattr(tweet.user, "verified", False) or getattr(tweet.user, "blueVerified", False)

        # Confidence formula
        vader_conf = abs(compound)  # 0.0 a 1.0
        log2_eng = math.log2(max(engagement, 1) + 1) / math.log2(MAX_ENGAGEMENT_FOR_SCALE + 1) if engagement > 0 else 0.0
        log2_eng = min(log2_eng, 1.0)
        verified_bonus = 1.0 if verified else 0.0

        confidence = vader_conf * 0.65 + log2_eng * 0.30 + verified_bonus * 0.05
        confidence = min(confidence, TWITTER_CONFIDENCE_CAP)

        # Sentiment label
        if compound > 0.15:
            sentiment_label = "positive"
        elif compound < -0.15:
            sentiment_label = "negative"
        else:
            sentiment_label = "neutral"

        # URL tweet
        tweet_id = getattr(tweet, "id", "")
        url = f"https://x.com/{username}/status/{tweet_id}" if tweet_id else ""

        return NewsArticle(
            title=text[:200] if text else "",
            summary="",
            source=f"twitter/@{username}",
            url=url,
            sentiment=sentiment_label,
            confidence=confidence,
            published_at=str(getattr(tweet, "date", "")),
            tickers=[],
            companies=[],
        )

    # ── Circuit breaker ───────────────────────────────────────

    def _check_circuit_breaker(self) -> bool:
        """Controlla se il circuit breaker è scattato. Se il cooldown è passato, resetta."""
        if self._available is not False:
            return True
        if self._circuit_breaker_trips == 0:
            return False  # disabilitato per assenza credenziali

        idx = min(self._circuit_breaker_trips - 1, len(CIRCUIT_BREAKER_COOLDOWNS) - 1)
        cooldown = CIRCUIT_BREAKER_COOLDOWNS[max(idx, 0)]
        elapsed = time.time() - self._circuit_breaker_at
        if elapsed >= cooldown:
            logger.info(
                f"[TWITTER] Circuit breaker reset dopo {elapsed:.0f}s "
                f"(trip #{self._circuit_breaker_trips}) — retry"
            )
            self._available = None
            self._consecutive_errors = 0
            return True
        return False

    def _register_error(self):
        """Circuit breaker: disabilita dopo MAX_CONSECUTIVE_ERRORS errori."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            self._circuit_breaker_at = time.time()
            self._circuit_breaker_trips += 1
            idx = min(self._circuit_breaker_trips - 1, len(CIRCUIT_BREAKER_COOLDOWNS) - 1)
            cooldown = CIRCUIT_BREAKER_COOLDOWNS[max(idx, 0)]
            logger.warning(
                f"[TWITTER] {self._consecutive_errors} errori consecutivi — "
                f"feed disabilitato (circuit breaker trip #{self._circuit_breaker_trips}, "
                f"retry tra {cooldown}s)"
            )
            self._available = False

    # ── Utility ───────────────────────────────────────────────

    @staticmethod
    def _question_to_query(question: str) -> str:
        """Converte una domanda Polymarket in una query Twitter."""
        stop_words = {
            "will", "the", "be", "by", "on", "in", "at", "to", "of",
            "a", "an", "is", "or", "and", "for", "this", "that", "it",
            "yes", "no", "before", "after", "end", "day",
        }
        words = question.lower().split()
        key_words = [w for w in words if w not in stop_words and len(w) > 2]

        if len(key_words) < 2:
            return ""

        # Primi 5 keyword per query concisa
        return " ".join(key_words[:5])
