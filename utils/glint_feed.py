"""
Feed Glint.trade — Real-time intelligence per prediction markets.

Glint fornisce segnali news/social (Bloomberg, Reuters, Twitter, Telegram, OSINT)
e li mappa automaticamente su contratti Polymarket con relevance scoring AI.

Protocollo WS reverse-engineered:
- wss://api.glint.trade/ws → auth JWT a 2 livelli → subscribe room "feed"
- Messaggi: type:"new" (segnale) → type:"related_markets" (mercati matchati),
  correlati via feed_item_id

Più ricco di GDELT+Finlight:
- relevance_score (1-10) pre-calcolato AI
- impact_level (low/medium/high)
- impact_reason AI-generated

Env var: GLINT_SESSION_TOKEN (session JWT dal browser, ~7gg)
Senza token: feed disabilitato (noop), bot gira normalmente.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

import requests

# ── Configurazione ──────────────────────────────────────────────

GLINT_WS_URL = "wss://api.glint.trade/ws"
GLINT_API_BASE = "https://api.glint.trade"

# Token refresh: WS JWT dura ~5min, refreshiamo ogni 4min
WS_TOKEN_REFRESH_INTERVAL = 240  # 4 minuti

# Correlation: segnale + related_markets arrivano separati via feed_item_id
PENDING_SIGNAL_TIMEOUT = 30  # secondi max per aspettare related_markets

# Filtro qualità: solo segnali con relevance >= 5 (su scala 1-10)
MIN_RELEVANCE_SCORE = 5

# Max segnali in coda (evita memory leak se il consumer è lento)
MAX_QUEUE_SIZE = 200

# Mapping categorie Glint → CATEGORY_CONFIG keys in event_driven.py
GLINT_CATEGORY_MAP: dict[str, str] = {
    "politics": "political",
    "political": "political",
    "election": "political",
    "elections": "political",
    "government": "political",
    "crypto": "crypto_regulatory",
    "cryptocurrency": "crypto_regulatory",
    "regulation": "crypto_regulatory",
    "blockchain": "crypto_regulatory",
    "defi": "crypto_regulatory",
    "geopolitics": "geopolitical",
    "geopolitical": "geopolitical",
    "war": "geopolitical",
    "conflict": "geopolitical",
    "international": "geopolitical",
    "macro": "macro",
    "economy": "macro",
    "economic": "macro",
    "fed": "macro",
    "inflation": "macro",
    "rates": "macro",
    "tech": "tech",
    "technology": "tech",
    "ai": "tech",
    "earnings": "tech",
}

# Keywords per inferire sentiment da impact_reason
POSITIVE_KEYWORDS = {
    "increase", "rise", "gain", "surge", "boost", "bullish",
    "positive", "approval", "approve", "pass", "win", "victory",
    "support", "growth", "recover", "advance", "strong", "higher",
    "upgrade", "succeed", "favorable", "optimistic", "rally",
}
NEGATIVE_KEYWORDS = {
    "decrease", "fall", "drop", "crash", "decline", "bearish",
    "negative", "reject", "fail", "loss", "defeat", "ban",
    "oppose", "recession", "collapse", "weak", "lower", "risk",
    "downgrade", "concern", "threat", "warning", "sell-off",
}


@dataclass
class GlintSignal:
    """Segnale raw da Glint (type:new)."""
    feed_item_id: str = ""
    title: str = ""
    summary: str = ""
    source: str = ""
    category: str = ""
    impact_level: str = "low"      # low / medium / high
    impact_reason: str = ""
    timestamp: float = 0.0


@dataclass
class GlintMarketMatch:
    """Mercato Polymarket matchato da Glint (type:related_markets)."""
    condition_id: str = ""
    slug: str = ""
    question: str = ""
    relevance_score: float = 0.0   # 1-10
    current_price_yes: float = 0.0


@dataclass
class GlintOpportunity:
    """Opportunità completa: segnale + mercato matchato, pronta per il consumer."""
    signal: GlintSignal
    market_match: GlintMarketMatch
    event_type: str = ""           # Mapped CATEGORY_CONFIG key
    inferred_sentiment: float = 0.0  # -1.0 a +1.0
    received_at: float = 0.0


@dataclass
class GlintFeed:
    """
    WebSocket client async per Glint.trade feed.

    Pattern: come binance_feed.py — backoff esponenziale con jitter,
    graceful degradation senza token.

    Output duale:
    - drain_opportunities() → list[GlintOpportunity] (ricco, market match pre-computed)
    - get_event_sentiment() / get_news_strength() / detect_breaking_news() →
      stessa interfaccia Finlight/GDELT per compatibilità merge multi-fonte
    """
    _session_token: str = ""
    _ws_token: str = ""
    _ws_token_expires: float = 0.0
    _running: bool = False
    _disabled: bool = False        # True se 401 → token scaduto
    _connected: bool = False
    _consecutive_disconnects: int = 0

    # Two-phase correlation: feed_item_id → GlintSignal
    _pending_signals: dict = field(default_factory=dict)

    # Coda opportunità pronte per il consumer
    _opportunities: deque = field(default_factory=lambda: deque(maxlen=MAX_QUEUE_SIZE))

    # Cache per interfaccia Finlight/GDELT-compatibile
    # event_type → list[GlintOpportunity] (ultimi 5 min)
    _recent_by_category: dict = field(default_factory=dict)
    _last_cleanup: float = 0.0

    def __post_init__(self):
        self._session_token = os.environ.get("GLINT_SESSION_TOKEN", "").strip()
        if not self._session_token:
            logger.info("[GLINT] GLINT_SESSION_TOKEN non configurato — feed disabilitato")
            self._disabled = True

    @property
    def available(self) -> bool:
        return not self._disabled and bool(self._session_token)

    @property
    def connected(self) -> bool:
        return self._connected

    # ── WebSocket Connection ───────────────────────────────────

    async def connect(self):
        """Connessione WS con backoff esponenziale + jitter (pattern binance_feed.py)."""
        if self._disabled or not self._session_token:
            return

        if not HAS_WEBSOCKETS:
            logger.warning("[GLINT] websockets non installato — feed disabilitato")
            self._disabled = True
            return

        self._running = True

        while self._running:
            try:
                # Refresh WS token prima di connettersi
                if not self._refresh_ws_token():
                    # 401 o errore → aspetta e riprova (o disabled)
                    if self._disabled:
                        return
                    await asyncio.sleep(30)
                    continue

                async with websockets.connect(
                    f"{GLINT_WS_URL}?token={self._ws_token}",
                    ping_interval=20,
                    ping_timeout=30,
                    close_timeout=10,
                ) as ws:
                    self._consecutive_disconnects = 0
                    self._connected = True
                    logger.info("[GLINT] WebSocket connesso")

                    # Subscribe alla room feed
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "room": "feed",
                    }))

                    # Task per token refresh periodico
                    refresh_task = asyncio.create_task(
                        self._token_refresh_loop(ws)
                    )

                    try:
                        async for msg in ws:
                            if not self._running:
                                break
                            self._handle_message(msg)
                    finally:
                        refresh_task.cancel()

            except Exception as e:
                self._connected = False
                self._consecutive_disconnects += 1

                if "401" in str(e) or "403" in str(e):
                    logger.warning(
                        "[GLINT] Session token scaduto (401) — "
                        "aggiorna GLINT_SESSION_TOKEN e riavvia il bot"
                    )
                    self._disabled = True
                    return

                import random
                backoff = min(2 * (2 ** (self._consecutive_disconnects - 1)), 30)
                jitter = random.uniform(0, backoff * 0.3)
                wait = backoff + jitter
                logger.warning(
                    f"[GLINT] Disconnesso (#{self._consecutive_disconnects}), "
                    f"riconnessione in {wait:.1f}s... ({e})"
                )
                await asyncio.sleep(wait)

        self._connected = False

    async def _token_refresh_loop(self, ws):
        """Refresh WS token ogni 4 minuti (JWT dura ~5min)."""
        while self._running:
            await asyncio.sleep(WS_TOKEN_REFRESH_INTERVAL)
            if not self._running:
                break
            if self._refresh_ws_token():
                try:
                    await ws.send(json.dumps({
                        "type": "auth_refresh",
                        "token": self._ws_token,
                    }))
                except Exception:
                    break  # WS chiuso, riconnessione gestita dal loop principale

    def _refresh_ws_token(self) -> bool:
        """
        GET /api/auth/ws-token con Bearer session JWT → WS JWT (~5min).
        Returns True se OK, False se errore. Setta self._disabled su 401.
        """
        if self._disabled:
            return False

        # Se il token WS è ancora valido, non refreshare
        if self._ws_token and time.time() < self._ws_token_expires - 30:
            return True

        try:
            resp = requests.get(
                f"{GLINT_API_BASE}/api/auth/ws-token",
                headers={"Authorization": f"Bearer {self._session_token}"},
                timeout=10,
            )

            if resp.status_code == 401:
                logger.warning(
                    "[GLINT] Session token scaduto (401) — "
                    "aggiorna GLINT_SESSION_TOKEN e riavvia il bot"
                )
                self._disabled = True
                return False

            if resp.status_code != 200:
                logger.warning(
                    f"[GLINT] ws-token HTTP {resp.status_code}"
                )
                return False

            data = resp.json()
            self._ws_token = data.get("token", "")
            if not self._ws_token:
                logger.warning("[GLINT] ws-token response vuota")
                return False

            self._ws_token_expires = time.time() + 300  # ~5 min
            return True

        except Exception as e:
            logger.warning(f"[GLINT] Errore refresh ws-token: {e}")
            return False

    # ── Message Handling ───────────────────────────────────────

    def _handle_message(self, raw_msg: str):
        """Parse e gestisci messaggi WS Glint."""
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", "")

        if msg_type == "new":
            self._handle_new_signal(data)
        elif msg_type == "related_markets":
            self._handle_related_markets(data)

        # Cleanup pending signals scaduti
        self._cleanup_pending()

    def _handle_new_signal(self, data: dict):
        """Gestisce type:new — crea GlintSignal e mette in pending."""
        feed_item_id = data.get("feed_item_id", data.get("id", ""))
        if not feed_item_id:
            return

        signal = GlintSignal(
            feed_item_id=feed_item_id,
            title=data.get("title", ""),
            summary=data.get("summary", data.get("description", "")),
            source=data.get("source", ""),
            category=data.get("category", "").lower(),
            impact_level=data.get("impact_level", "low").lower(),
            impact_reason=data.get("impact_reason", ""),
            timestamp=time.time(),
        )

        self._pending_signals[feed_item_id] = signal

    def _handle_related_markets(self, data: dict):
        """Gestisce type:related_markets — correla con segnale pending via feed_item_id."""
        feed_item_id = data.get("feed_item_id", "")
        signal = self._pending_signals.pop(feed_item_id, None)

        if not signal:
            return

        markets = data.get("markets", data.get("related_markets", []))
        if not markets:
            return

        for mkt in markets:
            relevance = float(mkt.get("relevance_score", mkt.get("relevance", 0)))

            # Filtro qualità
            if relevance < MIN_RELEVANCE_SCORE:
                continue

            match = GlintMarketMatch(
                condition_id=mkt.get("condition_id", ""),
                slug=mkt.get("slug", ""),
                question=mkt.get("question", mkt.get("title", "")),
                relevance_score=relevance,
                current_price_yes=float(mkt.get("price_yes", mkt.get("current_price", 0))),
            )

            # Map categoria
            event_type = self._map_category(signal.category)

            # Inferisci sentiment
            sentiment = self._infer_sentiment(signal.impact_reason, signal.title)

            opp = GlintOpportunity(
                signal=signal,
                market_match=match,
                event_type=event_type,
                inferred_sentiment=sentiment,
                received_at=time.time(),
            )

            self._opportunities.append(opp)
            self._add_to_recent(opp)

            logger.info(
                f"[GLINT] Match: '{signal.title[:50]}' "
                f"relevance={relevance:.0f} impact={signal.impact_level} "
                f"→ '{match.question[:40]}' sent={sentiment:+.2f}"
            )

    def _cleanup_pending(self):
        """Rimuovi segnali pending scaduti (> PENDING_SIGNAL_TIMEOUT)."""
        now = time.time()
        expired = [
            fid for fid, sig in self._pending_signals.items()
            if now - sig.timestamp > PENDING_SIGNAL_TIMEOUT
        ]
        for fid in expired:
            del self._pending_signals[fid]

    # ── Category Mapping ───────────────────────────────────────

    def _map_category(self, glint_category: str) -> str:
        """Mappa categoria Glint → CATEGORY_CONFIG key di event_driven."""
        cat = glint_category.lower().strip()

        # Match diretto
        if cat in GLINT_CATEGORY_MAP:
            return GLINT_CATEGORY_MAP[cat]

        # Match parziale (keyword nel nome della categoria)
        for keyword, event_type in GLINT_CATEGORY_MAP.items():
            if keyword in cat:
                return event_type

        return ""

    def _infer_sentiment(self, impact_reason: str, title: str) -> float:
        """
        Inferisci sentiment da impact_reason keywords.
        Returns: -1.0 a +1.0
        """
        text = f"{impact_reason} {title}".lower()
        words = set(text.split())

        pos_count = len(words & POSITIVE_KEYWORDS)
        neg_count = len(words & NEGATIVE_KEYWORDS)

        total = pos_count + neg_count
        if total == 0:
            return 0.0

        # Sentiment normalizzato: (pos - neg) / total, range [-1, +1]
        raw = (pos_count - neg_count) / total
        # Scala a max ±0.8 (non pretendiamo certezza dall'inferenza keyword)
        return max(min(raw * 0.8, 0.8), -0.8)

    # ── Output: Drain Queue ────────────────────────────────────

    def drain_opportunities(self) -> list[GlintOpportunity]:
        """
        Svuota la coda di opportunità pronte.
        Chiamato da event_driven._check_glint_opportunities().
        """
        opps = list(self._opportunities)
        self._opportunities.clear()
        return opps

    # ── Output: Interfaccia Finlight/GDELT-compatibile ─────────

    def _add_to_recent(self, opp: GlintOpportunity):
        """Aggiunge opportunità alla cache recente per categoria."""
        et = opp.event_type
        if not et:
            return
        if et not in self._recent_by_category:
            self._recent_by_category[et] = []
        self._recent_by_category[et].append(opp)

        # Cleanup ogni 60s: rimuovi entries > 5 min
        now = time.time()
        if now - self._last_cleanup > 60:
            for cat in list(self._recent_by_category):
                self._recent_by_category[cat] = [
                    o for o in self._recent_by_category[cat]
                    if now - o.received_at < 300
                ]
                if not self._recent_by_category[cat]:
                    del self._recent_by_category[cat]
            self._last_cleanup = now

    def get_event_sentiment(self, event_type: str) -> float:
        """
        Sentiment medio per categoria dagli ultimi 5 min.
        Returns: -1.0 a +1.0 (0.0 se nessun dato).
        """
        opps = self._recent_by_category.get(event_type, [])
        now = time.time()
        fresh = [o for o in opps if now - o.received_at < 300]
        if not fresh:
            return 0.0
        return sum(o.inferred_sentiment for o in fresh) / len(fresh)

    def get_news_strength(self, event_type: str) -> float:
        """
        Forza del segnale per categoria: 0.0 a 1.0.
        Combina volume segnali, impact_level e relevance.
        """
        opps = self._recent_by_category.get(event_type, [])
        now = time.time()
        fresh = [o for o in opps if now - o.received_at < 300]
        if not fresh:
            return 0.0

        # Volume score: 1 segnale=0.2, 3=0.6, 5+=1.0
        vol_score = min(len(fresh) / 5.0, 1.0)

        # Impact score: high=1.0, medium=0.6, low=0.3
        impact_map = {"high": 1.0, "medium": 0.6, "low": 0.3}
        impact_scores = [
            impact_map.get(o.signal.impact_level, 0.3) for o in fresh
        ]
        avg_impact = sum(impact_scores) / len(impact_scores)

        # Relevance score: media normalizzata (1-10 → 0-1)
        avg_relevance = sum(
            o.market_match.relevance_score for o in fresh
        ) / len(fresh) / 10.0

        # Sentiment strength
        avg_sent = abs(sum(o.inferred_sentiment for o in fresh) / len(fresh))

        strength = (
            vol_score * 0.25
            + avg_impact * 0.30
            + avg_relevance * 0.25
            + avg_sent * 0.20
        )
        return min(strength, 1.0)

    def detect_breaking_news(
        self, min_articles: int = 1, min_sentiment: float = 0.20
    ) -> list[tuple[str, float, float]]:
        """
        Rileva breaking news da Glint per categoria.
        Returns: [(event_type, avg_sentiment, n_signals)] ordinato per forza segnale.
        Interfaccia semplificata per _merge_breaking_news().
        """
        breaking = []
        now = time.time()

        for event_type, opps in self._recent_by_category.items():
            fresh = [o for o in opps if now - o.received_at < 300]
            if len(fresh) < min_articles:
                continue

            avg_sent = sum(o.inferred_sentiment for o in fresh) / len(fresh)
            if abs(avg_sent) < min_sentiment:
                continue

            breaking.append((event_type, avg_sent, len(fresh)))

        breaking.sort(key=lambda x: abs(x[1]) * x[2], reverse=True)

        if breaking:
            logger.info(
                f"[GLINT] BREAKING: {len(breaking)} categorie — "
                + " | ".join(
                    f"{et}: sent={s:+.2f}({n}sig)"
                    for et, s, n in breaking
                )
            )

        return breaking

    async def stop(self):
        """Ferma il feed."""
        self._running = False
        self._connected = False
