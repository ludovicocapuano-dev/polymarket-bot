"""
Feed Twitter/X — Breaking news via API ufficiale v2 per event_driven strategy.

Twitter/X è la fonte più veloce per breaking political/crypto news.
Complementa Finlight, GDELT e Glint come 4a fonte nel merge multi-sorgente.

Auth: Bearer Token (API v2 Basic plan, $200/mese).
Sentiment: VADER (già nel progetto). Confidence cappata a 0.80, strength discount 15%.

Senza TWITTER_BEARER_TOKEN env var → feed disabilitato, noop.
"""

import logging
import math
import os
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

# ── Configurazione ────────────────────────────────────────────

CACHE_TTL = 180  # 3 minuti, uguale a Finlight/GDELT
RATE_LIMIT_INTERVAL = 300  # 5 min per categoria (15K read/mese = ~2 req/h budget)

# Discount vs Finlight: VADER meno preciso di NLP fine-tuned
TWITTER_STRENGTH_DISCOUNT = 0.85  # 15% discount

# Confidence cap: VADER su tweet brevi è meno affidabile
TWITTER_CONFIDENCE_CAP = 0.80

# Circuit breaker
MAX_CONSECUTIVE_ERRORS = 5
CIRCUIT_BREAKER_COOLDOWNS = [60, 180, 300, 600]  # escalante

# Engagement: soglia per log2 scaling
MAX_ENGAGEMENT_FOR_SCALE = 100_000  # log2(100K) ≈ 16.6

# API v2 endpoint
TWITTER_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

# ── Import condizionale ──────────────────────────────────────

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
    Client Twitter/X via API v2 ufficiale — breaking news con VADER sentiment.

    Produce gli stessi tipi (NewsArticle, NewsSentiment) di FinlightFeed/GDELTFeed
    per permettere merge trasparente in event_driven.
    Degrada gracefully se VADER manca o Bearer Token assente.
    """

    _available: bool | None = None  # None=non testato, True=ok, False=disabilitato
    _cache: dict[str, NewsSentiment] = field(default_factory=dict)
    _rate_limit_at: dict[str, float] = field(default_factory=dict)  # categoria → last fetch
    _consecutive_errors: int = 0
    _circuit_breaker_at: float = 0.0
    _circuit_breaker_trips: int = 0
    _bearer_token: str = ""
    _vader: object = None  # SentimentIntensityAnalyzer
    _session: requests.Session | None = None

    def __post_init__(self):
        if not _HAS_VADER:
            logger.info("[TWITTER] vaderSentiment non installato — feed disabilitato")
            self._available = False
            return

        # Bearer Token da env var
        self._bearer_token = os.environ.get("TWITTER_BEARER_TOKEN", "")
        if not self._bearer_token:
            logger.info("[TWITTER] Nessun TWITTER_BEARER_TOKEN — feed disabilitato")
            self._available = False
            return

        # Init VADER
        self._vader = SentimentIntensityAnalyzer()

        # Init HTTP session
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._bearer_token}",
            "User-Agent": "PolymarketBot/1.0",
        })

        logger.info(
            f"[TWITTER] Feed inizializzato (API v2 Bearer) — "
            f"{len(TWITTER_QUERIES)} categorie"
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
        if self._available is False:
            return NewsSentiment(event_type=event_type)

        cache_key = f"mkt_{hash(question) % 10000}_{event_type}"
        cached = self._cache.get(cache_key)
        if cached and cached.is_fresh:
            return cached

        query = self._question_to_query(question)
        if not query:
            return NewsSentiment(event_type=event_type)

        articles = self._fetch_tweets(query, max_tweets=10)
        ns = NewsSentiment(
            event_type=event_type,
            articles=articles,
            fetched_at=time.time(),
        )
        self._cache[cache_key] = ns
        return ns

    def get_news_strength(self, event_type: str) -> float:
        ns = self.get_event_sentiment(event_type)
        if not ns.is_fresh or ns.n_articles == 0:
            return 0.0

        vol_score = min(ns.n_articles / 10.0, 1.0)
        sent_score = min(abs(ns.avg_sentiment) / 0.6, 1.0)

        hc = ns.high_confidence_sentiment
        hc_boost = 0.0
        if abs(hc) > 0.5 and ns.n_articles >= 3:
            hc_boost = 0.15

        strength = vol_score * 0.4 + sent_score * 0.5 + hc_boost
        strength *= TWITTER_STRENGTH_DISCOUNT
        return min(strength, 1.0)

    def detect_breaking_news(
        self, min_articles: int = 3, min_sentiment: float = 0.25
    ) -> list[tuple[str, NewsSentiment]]:
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

    # ── Fetch da Twitter API v2 ──────────────────────────────

    def _fetch_event_tweets(self, event_type: str):
        if not self._check_circuit_breaker():
            return

        now = time.time()
        last = self._rate_limit_at.get(event_type, 0.0)
        if now - last < RATE_LIMIT_INTERVAL:
            return

        queries = TWITTER_QUERIES.get(event_type, [])
        query = queries[0] if queries else ""
        if not query:
            return

        articles = self._fetch_tweets(query, max_tweets=10)
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
            logger.info(
                f"[TWITTER] {event_type}: {len(unique)} tweet, "
                f"sentiment={ns.sentiment_label} ({ns.avg_sentiment:+.2f})"
            )

    def _fetch_tweets(self, query: str, max_tweets: int = 10) -> list[NewsArticle]:
        """Cerca tweet via Twitter API v2 Recent Search."""
        if not self._check_circuit_breaker():
            return []

        try:
            params = {
                "query": f"{query} -is:retweet lang:en",
                "max_results": min(max_tweets, 100),  # API v2: 10-100
                "tweet.fields": "created_at,public_metrics,author_id",
                "expansions": "author_id",
                "user.fields": "username,verified",
            }

            resp = self._session.get(
                TWITTER_SEARCH_URL,
                params=params,
                timeout=15,
            )

            # Rate limit handling
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("retry-after", "60"))
                logger.warning(f"[TWITTER] Rate limit 429 — retry tra {retry_after}s")
                self._register_error()
                return []

            if resp.status_code == 401:
                logger.warning("[TWITTER] Bearer Token invalido (401) — feed disabilitato")
                self._available = False
                return []

            if resp.status_code == 403:
                logger.warning("[TWITTER] Accesso negato (403) — verificare piano API")
                self._register_error()
                return []

            resp.raise_for_status()
            data = resp.json()

            # Parse users map
            users_map: dict[str, dict] = {}
            for u in data.get("includes", {}).get("users", []):
                users_map[u["id"]] = u

            # Parse tweets
            articles = []
            for tweet in data.get("data", []):
                article = self._tweet_to_article(tweet, users_map)
                articles.append(article)

            # Reset errori su successo
            self._consecutive_errors = 0
            if self._available is None:
                self._available = True
                logger.info("[TWITTER] API v2 connessa — tweet disponibili")

            return articles

        except requests.exceptions.Timeout:
            logger.warning("[TWITTER] Timeout fetch")
            self._register_error()
            return []
        except Exception as e:
            logger.warning(f"[TWITTER] Errore fetch: {e}")
            self._register_error()
            return []

    # ── Parsing tweet → NewsArticle ───────────────────────────

    def _tweet_to_article(self, tweet: dict, users_map: dict) -> NewsArticle:
        """Converte un tweet API v2 in NewsArticle con VADER sentiment."""
        text = tweet.get("text", "")
        tweet_id = tweet.get("id", "")
        author_id = tweet.get("author_id", "")

        # Lookup username
        user_info = users_map.get(author_id, {})
        username = user_info.get("username", "unknown")
        verified = user_info.get("verified", False)

        # VADER sentiment
        vader_scores = self._vader.polarity_scores(text)
        compound = vader_scores["compound"]

        # Engagement
        metrics = tweet.get("public_metrics", {})
        likes = metrics.get("like_count", 0)
        retweets = metrics.get("retweet_count", 0)
        replies = metrics.get("reply_count", 0)
        engagement = likes + retweets + replies

        # Confidence formula
        vader_conf = abs(compound)
        log2_eng = (
            math.log2(max(engagement, 1) + 1) / math.log2(MAX_ENGAGEMENT_FOR_SCALE + 1)
            if engagement > 0 else 0.0
        )
        log2_eng = min(log2_eng, 1.0)
        verified_bonus = 1.0 if verified else 0.0

        confidence = vader_conf * 0.65 + log2_eng * 0.30 + verified_bonus * 0.05
        confidence = min(confidence, TWITTER_CONFIDENCE_CAP)

        if compound > 0.15:
            sentiment_label = "positive"
        elif compound < -0.15:
            sentiment_label = "negative"
        else:
            sentiment_label = "neutral"

        url = f"https://x.com/{username}/status/{tweet_id}" if tweet_id else ""

        return NewsArticle(
            title=text[:200] if text else "",
            summary="",
            source=f"twitter/@{username}",
            url=url,
            sentiment=sentiment_label,
            confidence=confidence,
            published_at=tweet.get("created_at", ""),
            tickers=[],
            companies=[],
        )

    # ── Circuit breaker ───────────────────────────────────────

    def _check_circuit_breaker(self) -> bool:
        if self._available is not False:
            return True
        if self._circuit_breaker_trips == 0:
            return False

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
        stop_words = {
            "will", "the", "be", "by", "on", "in", "at", "to", "of",
            "a", "an", "is", "or", "and", "for", "this", "that", "it",
            "yes", "no", "before", "after", "end", "day",
        }
        words = question.lower().split()
        key_words = [w for w in words if w not in stop_words and len(w) > 2]

        if len(key_words) < 2:
            return ""

        return " ".join(key_words[:5])
