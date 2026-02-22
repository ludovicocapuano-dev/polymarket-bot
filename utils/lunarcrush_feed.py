"""
Feed LunarCrush API v4 — Sentiment sociale e Galaxy Score per crypto.

Fornisce dati di sentiment social (Twitter/X, Reddit, YouTube, TikTok, etc.)
e il Galaxy Score composito per BTC, ETH, SOL, XRP.

LunarCrush API v4:
- Base URL: https://lunarcrush.com/api4
- Auth: Authorization: Bearer <key>
- Topic-based: "bitcoin", "ethereum", "solana", "xrp"

Metriche chiave:
- galaxy_score (0-100): composito di price + social + sentiment + correlation
- sentiment (0-100): % bullish pesata per interazioni
- alt_rank: ranking alternativo (1 = miglior segnale social)
- social_dominance: % della attivita' sociale totale
- interactions_24h: totale interazioni social nelle ultime 24h

Uso nel bot:
- crypto_5min: sentiment boost/penalty sul segnale di momentum
- data_driven: Galaxy Score come modificatore di probabilita'
"""

import logging
import os
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

# ── Mapping simbolo → topic LunarCrush ──────────────────────────
SYMBOL_TOPICS: dict[str, str] = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "xrp",
}

BASE_URL = "https://lunarcrush.com/api4"

# Cache durata in secondi — sentiment non cambia ogni secondo
CACHE_TTL = 120  # 2 minuti


@dataclass
class CryptoSentiment:
    """Dati di sentiment per un singolo simbolo crypto."""
    symbol: str                  # "btc", "eth", "sol", "xrp"
    galaxy_score: float = 0.0   # 0-100 (composito di price+social+sentiment)
    sentiment: float = 50.0     # 0-100 (% bullish pesata per interazioni)
    alt_rank: int = 0           # Ranking alternativo (1 = top)
    social_dominance: float = 0.0   # % di attivita' social vs totale
    interactions_24h: int = 0   # Interazioni social 24h
    contributors_24h: int = 0   # Numero di contributor unici 24h
    posts_24h: int = 0         # Post social 24h
    price_score: float = 0.0   # 0-5 (componente prezzo del galaxy score)
    social_score: float = 0.0  # 0-5 (componente social del galaxy score)
    fetched_at: float = 0.0    # Timestamp del fetch

    @property
    def is_fresh(self) -> bool:
        """True se i dati sono freschi (meno di CACHE_TTL secondi)."""
        return (time.time() - self.fetched_at) < CACHE_TTL if self.fetched_at > 0 else False

    @property
    def sentiment_signal(self) -> float:
        """
        Segnale di sentiment normalizzato tra -1.0 (ultra bearish) e +1.0 (ultra bullish).
        50 = neutro → 0.0
        80+ = molto bullish → +0.6 a +1.0
        20- = molto bearish → -0.6 a -1.0
        """
        if not self.is_fresh:
            return 0.0
        return (self.sentiment - 50.0) / 50.0

    @property
    def galaxy_signal(self) -> float:
        """
        Galaxy Score normalizzato tra -1.0 e +1.0.
        50 = neutro, 80+ = forte, 20- = debole.
        """
        if not self.is_fresh:
            return 0.0
        return (self.galaxy_score - 50.0) / 50.0

    @property
    def social_momentum(self) -> str:
        """Classificazione del momentum social."""
        if not self.is_fresh:
            return "UNKNOWN"
        if self.galaxy_score >= 70 and self.sentiment >= 65:
            return "STRONG_BULL"
        elif self.galaxy_score >= 55 and self.sentiment >= 55:
            return "MILD_BULL"
        elif self.galaxy_score <= 30 and self.sentiment <= 35:
            return "STRONG_BEAR"
        elif self.galaxy_score <= 45 and self.sentiment <= 45:
            return "MILD_BEAR"
        return "NEUTRAL"


@dataclass
class LunarCrushFeed:
    """
    Client per LunarCrush API v4.

    Fetcha sentiment e Galaxy Score per i simboli crypto supportati.
    Degrada gracefully se la API key manca o l'API e' irraggiungibile.
    """
    _api_key: str = ""
    _cache: dict[str, CryptoSentiment] = field(default_factory=dict)
    _session: requests.Session | None = None
    _available: bool | None = None  # None = non testato, True/False
    _last_error: str = ""

    def __post_init__(self):
        self._api_key = os.environ.get("LUNARCRUSH_API_KEY", "")
        if not self._api_key:
            logger.info("[LUNAR] Nessuna API key — sentiment disabilitato")
            self._available = False
            return

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": "PolymarketBot/3.7",
        })
        # Timeout aggressivo per non bloccare il bot
        self._session.timeout = 8

        # Inizializza cache vuota per tutti i simboli
        for sym in SYMBOL_TOPICS:
            self._cache[sym] = CryptoSentiment(symbol=sym)

        logger.info(
            f"[LUNAR] Feed inizializzato — "
            f"{len(SYMBOL_TOPICS)} simboli: {', '.join(s.upper() for s in SYMBOL_TOPICS)}"
        )

    # ── Accesso dati ─────────────────────────────────────────────

    def get_sentiment(self, symbol: str) -> CryptoSentiment:
        """
        Ottieni i dati di sentiment per un simbolo.
        Se i dati sono in cache e freschi, li ritorna senza chiamate API.
        Altrimenti fetcha dall'API.

        Ritorna CryptoSentiment con valori default se non disponibile.
        """
        sym = symbol.lower()
        if sym not in SYMBOL_TOPICS:
            return CryptoSentiment(symbol=sym)

        cached = self._cache.get(sym)
        if cached and cached.is_fresh:
            return cached

        # Fetch dall'API
        self._fetch_topic(sym)
        return self._cache.get(sym, CryptoSentiment(symbol=sym))

    def get_all_sentiments(self) -> dict[str, CryptoSentiment]:
        """
        Fetcha sentiment per TUTTI i simboli supportati.
        Usa il batch per efficienza (una request per simbolo,
        ma con cache che riduce le chiamate reali).
        """
        for sym in SYMBOL_TOPICS:
            self.get_sentiment(sym)
        return {sym: self._cache.get(sym, CryptoSentiment(symbol=sym))
                for sym in SYMBOL_TOPICS}

    def sentiment_summary(self) -> str:
        """Stringa riassuntiva per il log."""
        parts = []
        for sym in SYMBOL_TOPICS:
            cs = self._cache.get(sym)
            if cs and cs.is_fresh:
                parts.append(
                    f"{sym.upper()}: GS={cs.galaxy_score:.0f} "
                    f"Sent={cs.sentiment:.0f}% "
                    f"({cs.social_momentum})"
                )
            else:
                parts.append(f"{sym.upper()}: --")
        return " | ".join(parts)

    # ── API calls ────────────────────────────────────────────────

    def _fetch_topic(self, symbol: str):
        """
        Fetcha dati per un singolo simbolo da LunarCrush API v4.

        Endpoint: GET /public/topic/:topic/v1
        Ritorna dettagli del topic con sentiment, galaxy_score, etc.

        Fallback: se /v1 non funziona, prova /time-series/v2 con bucket=day.
        """
        if self._available is False:
            return

        topic = SYMBOL_TOPICS.get(symbol)
        if not topic or not self._session:
            return

        # ── Tentativo 1: Topic details (/public/topic/:topic/v1) ──
        try:
            url = f"{BASE_URL}/public/topic/{topic}/v1"
            resp = self._session.get(url, timeout=8)

            if resp.status_code == 200:
                data = resp.json()
                self._parse_topic_response(symbol, data)
                if self._available is None:
                    self._available = True
                    logger.info(f"[LUNAR] API connessa via topic/v1")
                return
            elif resp.status_code == 401:
                logger.warning("[LUNAR] API key non valida (401)")
                self._available = False
                return
            elif resp.status_code == 429:
                logger.warning("[LUNAR] Rate limit raggiunto (429)")
                return
            else:
                logger.debug(f"[LUNAR] topic/v1 HTTP {resp.status_code}")

        except requests.Timeout:
            logger.debug(f"[LUNAR] Timeout su topic/v1 per {symbol}")
        except requests.RequestException as e:
            logger.debug(f"[LUNAR] Errore topic/v1: {e}")

        # ── Tentativo 2: Time series (/public/topic/:topic/time-series/v2) ──
        try:
            url = f"{BASE_URL}/public/topic/{topic}/time-series/v2"
            params = {"bucket": "hour", "interval": "1d"}
            resp = self._session.get(url, params=params, timeout=8)

            if resp.status_code == 200:
                data = resp.json()
                self._parse_timeseries_response(symbol, data)
                if self._available is None:
                    self._available = True
                    logger.info(f"[LUNAR] API connessa via time-series/v2")
                return
            else:
                logger.debug(f"[LUNAR] time-series/v2 HTTP {resp.status_code}")

        except requests.Timeout:
            logger.debug(f"[LUNAR] Timeout su time-series/v2 per {symbol}")
        except requests.RequestException as e:
            logger.debug(f"[LUNAR] Errore time-series/v2: {e}")

        # ── Tentativo 3: endpoint alternativo (/public/coins/:topic/v1) ──
        try:
            url = f"{BASE_URL}/public/coins/{topic}/v1"
            resp = self._session.get(url, timeout=8)

            if resp.status_code == 200:
                data = resp.json()
                self._parse_topic_response(symbol, data)
                if self._available is None:
                    self._available = True
                    logger.info(f"[LUNAR] API connessa via coins/v1")
                return

        except requests.RequestException:
            pass

        # Se nessun endpoint funziona al primo tentativo
        if self._available is None:
            logger.warning(
                f"[LUNAR] Nessun endpoint funzionante — "
                f"sentiment sara' disponibile quando l'API rispondera'"
            )
            # NON setta _available=False: riprova al prossimo ciclo
            # (potrebbe essere un problema temporaneo di rete)

    def _parse_topic_response(self, symbol: str, data: dict):
        """
        Parsa la risposta di /public/topic/:topic/v1.

        La risposta puo' essere:
        - {"data": {...topic details...}}
        - {... topic details direttamente ...}
        - {"topic": {...}}
        """
        # Estrai il payload principale
        payload = data
        if "data" in data and isinstance(data["data"], dict):
            payload = data["data"]
        elif "topic" in data and isinstance(data["topic"], dict):
            payload = data["topic"]

        cs = CryptoSentiment(
            symbol=symbol,
            galaxy_score=self._safe_float(payload, "galaxy_score", 0.0),
            sentiment=self._safe_float(payload, "sentiment", 50.0),
            alt_rank=self._safe_int(payload, "alt_rank", 0),
            social_dominance=self._safe_float(payload, "social_dominance", 0.0),
            interactions_24h=self._safe_int(payload, "interactions_24h",
                                           self._safe_int(payload, "interactions", 0)),
            contributors_24h=self._safe_int(payload, "contributors_24h",
                                           self._safe_int(payload, "contributors", 0)),
            posts_24h=self._safe_int(payload, "posts_24h",
                                    self._safe_int(payload, "num_posts", 0)),
            price_score=self._safe_float(payload, "price_score", 0.0),
            social_score=self._safe_float(payload, "social_score", 0.0),
            fetched_at=time.time(),
        )

        # Normalizza sentiment: LunarCrush puo' dare 0-100 o 0-1
        if 0 < cs.sentiment <= 1.0:
            cs.sentiment *= 100

        # Normalizza galaxy_score: LunarCrush puo' dare 0-100 o 0-1
        if 0 < cs.galaxy_score <= 1.0:
            cs.galaxy_score *= 100

        self._cache[symbol] = cs
        logger.debug(
            f"[LUNAR] {symbol.upper()}: GS={cs.galaxy_score:.1f} "
            f"Sent={cs.sentiment:.1f}% AltRank={cs.alt_rank} "
            f"Social={cs.social_momentum}"
        )

    def _parse_timeseries_response(self, symbol: str, data: dict):
        """
        Parsa la risposta di /public/topic/:topic/time-series/v2.

        Il time-series ritorna una lista di punti dati.
        Usa l'ULTIMO punto per i dati piu' recenti.
        """
        # Trova la lista di datapoints
        points = data
        if "data" in data:
            points = data["data"]
        if "timeSeries" in data:
            points = data["timeSeries"]
        if "time_series" in data:
            points = data["time_series"]

        if not isinstance(points, list) or len(points) == 0:
            logger.debug(f"[LUNAR] Time-series vuota per {symbol}")
            return

        # Usa l'ultimo datapoint
        latest = points[-1]

        cs = CryptoSentiment(
            symbol=symbol,
            galaxy_score=self._safe_float(latest, "galaxy_score", 0.0),
            sentiment=self._safe_float(latest, "sentiment", 50.0),
            alt_rank=self._safe_int(latest, "alt_rank", 0),
            social_dominance=self._safe_float(latest, "social_dominance", 0.0),
            interactions_24h=self._safe_int(latest, "interactions", 0),
            contributors_24h=self._safe_int(latest, "contributors", 0),
            posts_24h=self._safe_int(latest, "num_posts", 0),
            price_score=self._safe_float(latest, "price_score", 0.0),
            social_score=self._safe_float(latest, "social_score", 0.0),
            fetched_at=time.time(),
        )

        if 0 < cs.sentiment <= 1.0:
            cs.sentiment *= 100
        if 0 < cs.galaxy_score <= 1.0:
            cs.galaxy_score *= 100

        self._cache[symbol] = cs
        logger.debug(
            f"[LUNAR] {symbol.upper()} (ts): GS={cs.galaxy_score:.1f} "
            f"Sent={cs.sentiment:.1f}%"
        )

    # ── Helper di parsing ────────────────────────────────────────

    @staticmethod
    def _safe_float(d: dict, key: str, default: float = 0.0) -> float:
        """Estrai un float da un dict, con fallback."""
        val = d.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_int(d: dict, key: str, default: int = 0) -> int:
        """Estrai un int da un dict, con fallback."""
        val = d.get(key)
        if val is None:
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default
