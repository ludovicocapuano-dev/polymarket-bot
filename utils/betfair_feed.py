"""
Betfair Exchange Streaming Feed — v1.0
======================================
Real-time odds streaming from Betfair Exchange for sport latency arbitrage.
Detects in-game events (goals, penalties, red cards) via odds shift patterns
before Polymarket updates its prices.

Architecture:
- betfairlightweight APIClient for auth + market discovery
- Streaming API via custom StreamListener for real-time odds updates
- Event classification by odds shift magnitude
- Thread-safe signal queue consumed by sport_latency strategy

Latency chain:
  Real event -> Sharp bettors move Betfair odds (1-3s) -> We detect shift ->
  Compare to stale Polymarket price (10-15s lag) -> Trade the gap
"""

import asyncio
import logging
import os
import time
import threading
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Betfair sport event type IDs
SPORT_EVENT_TYPES = {
    "1": "soccer",
    "7522": "basketball",
    "2": "tennis",
    "26420": "mma",
    "7511": "baseball",
    "6423": "american_football",
}

# Only track these market types (clearest signal for outcome changes)
MATCH_ODDS_TYPES = {"MATCH_ODDS", "MONEYLINE", "MATCH_WINNER"}

# Event detection thresholds (implied probability shift)
GOAL_THRESHOLD = 0.30       # goal / knockout
SET_PIECE_THRESHOLD = 0.15  # penalty / red card
MOMENTUM_THRESHOLD = 0.08   # sustained tactical advantage
NOISE_THRESHOLD = 0.05      # below this = ignore

# Anti-duplicate: minimum seconds between signals for same market
SIGNAL_COOLDOWN = 30.0

# Market discovery refresh interval
DISCOVERY_INTERVAL = 300.0  # 5 minutes


@dataclass
class OddsSnapshot:
    """Point-in-time snapshot of odds for one Betfair selection."""
    selection_id: int
    runner_name: str        # "Real Madrid", "Draw", "Atletico Madrid"
    back_odds: float        # best back price
    implied_prob: float     # 1 / back_odds (raw, before overround removal)
    timestamp: float = 0.0


@dataclass
class EventSignal:
    """Detected in-game event from odds movement."""
    event_type: str             # "goal", "red_card", "penalty", "momentum"
    betfair_event_id: str
    betfair_market_id: str
    event_name: str             # "Real Madrid v Atletico Madrid"
    competition: str
    sport: str
    team: str                   # runner name whose odds moved most
    direction: str              # "shortened" (odds dropped = more likely) or "drifted"
    timestamp: float
    odds_before: OddsSnapshot
    odds_after: OddsSnapshot
    implied_prob_shift: float   # absolute change in implied probability
    confidence: float           # 0-1
    all_selections: dict        # selection_id -> OddsSnapshot (post-event)


@dataclass
class BetfairMarketData:
    """Live state for one Betfair market."""
    market_id: str
    event_id: str
    event_name: str             # "Real Madrid v Atletico Madrid"
    competition: str
    sport: str
    in_play: bool = False
    selections: dict = field(default_factory=dict)  # selection_id -> OddsSnapshot
    last_signal_at: float = 0.0


@dataclass
class BetfairFeed:
    """
    Real-time Betfair Exchange odds feed for sport event detection.

    Connects to Betfair Streaming API via betfairlightweight.
    Detects in-game events from odds shift patterns.
    Produces EventSignal objects consumed by SportLatencyStrategy.

    Graceful degradation: if credentials are missing, feed is disabled (noop).
    """
    _running: bool = False
    _disabled: bool = False
    _markets: dict = field(default_factory=dict)        # market_id -> BetfairMarketData
    _event_queue: deque = field(default_factory=lambda: deque(maxlen=100))
    _consecutive_disconnects: int = 0
    _last_msg_at: float = 0.0
    _last_discovery_at: float = 0.0
    _client: object = field(default=None, repr=False)   # betfairlightweight.APIClient
    _stream: object = field(default=None, repr=False)
    _stream_thread: object = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        username = os.getenv("BETFAIR_USERNAME", "")
        password = os.getenv("BETFAIR_PASSWORD", "")
        app_key = os.getenv("BETFAIR_APP_KEY", "")
        certs_path = os.getenv("BETFAIR_CERTS_PATH", "")

        if not all([username, password, app_key]):
            logger.info("[BETFAIR] Credenziali mancanti — feed disabilitato (noop)")
            self._disabled = True
            return

        try:
            import betfairlightweight
            self._client = betfairlightweight.APIClient(
                username=username,
                password=password,
                app_key=app_key,
                certs=certs_path if certs_path else None,
            )
            logger.info("[BETFAIR] Client inizializzato")
        except Exception as e:
            logger.error(f"[BETFAIR] Errore init client: {e}")
            self._disabled = True

    def _login(self) -> bool:
        """Login to Betfair. Returns True on success."""
        if self._disabled or not self._client:
            return False
        try:
            self._client.login()
            logger.info("[BETFAIR] Login riuscito")
            return True
        except Exception as e:
            logger.error(f"[BETFAIR] Login fallito: {e}")
            return False

    def _discover_inplay_markets(self) -> list:
        """Find live in-play Match Odds markets on Betfair."""
        if self._disabled or not self._client:
            return []

        try:
            import betfairlightweight.filters as filters

            market_filter = filters.market_filter(
                event_type_ids=list(SPORT_EVENT_TYPES.keys()),
                in_play_only=True,
                market_type_codes=list(MATCH_ODDS_TYPES),
            )

            catalogues = self._client.betting.list_market_catalogue(
                filter=market_filter,
                market_projection=[
                    "COMPETITION",
                    "EVENT",
                    "RUNNER_DESCRIPTION",
                    "MARKET_START_TIME",
                ],
                max_results=50,
            )

            discovered = []
            for cat in catalogues:
                market_id = cat.market_id
                event = cat.event
                competition = cat.competition

                event_id = str(event.id) if event else ""
                event_name = event.name if event else ""
                comp_name = competition.name if competition else ""
                sport_id = str(cat.event_type.id) if cat.event_type else ""
                sport = SPORT_EVENT_TYPES.get(sport_id, "unknown")

                # Build market data
                md = BetfairMarketData(
                    market_id=market_id,
                    event_id=event_id,
                    event_name=event_name,
                    competition=comp_name,
                    sport=sport,
                    in_play=True,
                )

                # Store runner info
                if cat.runners:
                    for runner in cat.runners:
                        md.selections[runner.selection_id] = OddsSnapshot(
                            selection_id=runner.selection_id,
                            runner_name=runner.runner_name,
                            back_odds=0.0,
                            implied_prob=0.0,
                        )

                self._markets[market_id] = md
                discovered.append(market_id)

            logger.info(
                f"[BETFAIR] Scoperti {len(discovered)} mercati in-play "
                f"({', '.join(set(self._markets[m].sport for m in discovered))})"
            )
            self._last_discovery_at = time.time()
            return discovered

        except Exception as e:
            logger.error(f"[BETFAIR] Errore discovery: {e}")
            return []

    def _start_streaming(self, market_ids: list):
        """Start streaming odds for given market IDs in a background thread."""
        if not market_ids or self._disabled:
            return

        try:
            import betfairlightweight

            class OddsListener(betfairlightweight.StreamListener):
                def __init__(self, feed: "BetfairFeed"):
                    super().__init__(max_latency=0.5)
                    self.feed = feed

                def on_data(self, raw_data):
                    """Called by betfairlightweight on each streaming message."""
                    try:
                        self.feed._process_stream_data(raw_data)
                    except Exception as e:
                        logger.debug(f"[BETFAIR] Stream data error: {e}")
                    return super().on_data(raw_data)

            listener = OddsListener(self)
            self._stream = self._client.streaming.create_stream(
                listener=listener,
            )
            self._stream.subscribe_to_markets(
                market_filter=betfairlightweight.filters.streaming_market_filter(
                    market_ids=market_ids,
                ),
                market_data_filter=betfairlightweight.filters.streaming_market_data_filter(
                    fields=["EX_BEST_OFFERS", "EX_TRADED"],
                    ladder_levels=1,
                ),
            )
            self._stream.start(async_=True)
            logger.info(f"[BETFAIR] Streaming avviato su {len(market_ids)} mercati")

        except Exception as e:
            logger.error(f"[BETFAIR] Errore avvio streaming: {e}")

    def _process_stream_data(self, raw_data):
        """Process raw streaming data and detect events."""
        self._last_msg_at = time.time()

        # betfairlightweight parses MarketBook objects from the stream
        # We need to check the listener's output_list
        if not self._stream or not self._stream.listener:
            return

        snap = self._stream.listener.snap(
            market_ids=list(self._markets.keys())
        )
        if not snap:
            return

        for market_book in snap:
            market_id = market_book.market_id
            md = self._markets.get(market_id)
            if not md:
                continue

            md.in_play = getattr(market_book, 'inplay', False)
            if not md.in_play:
                continue

            for runner in market_book.runners:
                sel_id = runner.selection_id
                old_snap = md.selections.get(sel_id)
                if not old_snap:
                    continue

                # Get best back price
                new_back = 0.0
                if runner.ex and runner.ex.available_to_back:
                    new_back = runner.ex.available_to_back[0].price

                if new_back <= 0:
                    continue

                new_implied = 1.0 / new_back
                old_implied = old_snap.implied_prob

                # Create new snapshot
                new_snap = OddsSnapshot(
                    selection_id=sel_id,
                    runner_name=old_snap.runner_name,
                    back_odds=new_back,
                    implied_prob=new_implied,
                    timestamp=time.time(),
                )

                # Detect significant shift
                if old_implied > 0:
                    shift = abs(new_implied - old_implied)
                    if shift >= NOISE_THRESHOLD:
                        signal = self._classify_event(md, old_snap, new_snap, shift)
                        if signal:
                            with self._lock:
                                self._event_queue.append(signal)

                # Update stored snapshot
                md.selections[sel_id] = new_snap

    def _classify_event(
        self,
        md: BetfairMarketData,
        old_snap: OddsSnapshot,
        new_snap: OddsSnapshot,
        shift: float,
    ) -> EventSignal | None:
        """Classify an odds shift as an in-game event."""
        now = time.time()

        # Anti-duplicate: cooldown per market
        if now - md.last_signal_at < SIGNAL_COOLDOWN:
            return None

        # Classify by shift magnitude
        if shift >= GOAL_THRESHOLD:
            event_type = "goal"
            confidence = 0.95
        elif shift >= SET_PIECE_THRESHOLD:
            event_type = "set_piece"  # penalty or red card
            confidence = 0.80
        elif shift >= MOMENTUM_THRESHOLD:
            event_type = "momentum"
            confidence = 0.60
        else:
            return None  # below momentum threshold

        # Direction: did odds shorten (more likely) or drift (less likely)?
        direction = "shortened" if new_snap.implied_prob > old_snap.implied_prob else "drifted"

        # Build all_selections snapshot
        all_sels = {}
        for sel_id, snap in md.selections.items():
            all_sels[sel_id] = snap

        signal = EventSignal(
            event_type=event_type,
            betfair_event_id=md.event_id,
            betfair_market_id=md.market_id,
            event_name=md.event_name,
            competition=md.competition,
            sport=md.sport,
            team=new_snap.runner_name,
            direction=direction,
            timestamp=now,
            odds_before=old_snap,
            odds_after=new_snap,
            implied_prob_shift=shift,
            confidence=confidence,
            all_selections=all_sels,
        )

        md.last_signal_at = now

        logger.info(
            f"[BETFAIR-EVENT] {event_type.upper()} detected! "
            f"{md.event_name} | {new_snap.runner_name} {direction} "
            f"({old_snap.back_odds:.2f} -> {new_snap.back_odds:.2f}, "
            f"shift={shift:.3f}) | conf={confidence:.2f}"
        )

        return signal

    # ── Public API ─────────────────────────────────────────────

    async def connect(self):
        """Main connection loop. Login, discover, stream, reconnect."""
        if self._disabled:
            logger.info("[BETFAIR] Feed disabilitato (credenziali mancanti)")
            return

        self._running = True

        while self._running:
            try:
                if not self._login():
                    await asyncio.sleep(30)
                    continue

                market_ids = self._discover_inplay_markets()
                if not market_ids:
                    logger.info("[BETFAIR] Nessun mercato in-play, retry in 60s")
                    await asyncio.sleep(60)
                    continue

                self._start_streaming(market_ids)
                self._consecutive_disconnects = 0

                # Keep alive: periodically re-discover markets and check health
                while self._running:
                    await asyncio.sleep(30)

                    # Re-discover markets every 5 minutes
                    if time.time() - self._last_discovery_at > DISCOVERY_INTERVAL:
                        new_ids = self._discover_inplay_markets()
                        if new_ids and set(new_ids) != set(market_ids):
                            logger.info(f"[BETFAIR] Nuovi mercati, riavvio stream...")
                            if self._stream:
                                self._stream.stop()
                            market_ids = new_ids
                            self._start_streaming(market_ids)

                    # Stale check
                    if self.is_stale(120):
                        logger.warning("[BETFAIR] Stream stale, riconnessione...")
                        break

            except Exception as e:
                self._consecutive_disconnects += 1
                import random as _rng
                backoff = min(5 * (2 ** (self._consecutive_disconnects - 1)), 60)
                jitter = _rng.uniform(0, backoff * 0.3)
                wait = backoff + jitter
                logger.error(f"[BETFAIR] Errore: {e}, retry in {wait:.1f}s")
                if self._stream:
                    try:
                        self._stream.stop()
                    except Exception:
                        pass
                await asyncio.sleep(wait)

    def pop_signals(self) -> list[EventSignal]:
        """Thread-safe drain of event queue. Called by strategy each cycle."""
        with self._lock:
            signals = list(self._event_queue)
            self._event_queue.clear()
        return signals

    def get_market_state(self, market_id: str) -> BetfairMarketData | None:
        """Get current market data for a Betfair market."""
        return self._markets.get(market_id)

    def get_all_inplay(self) -> list[BetfairMarketData]:
        """Get all currently tracked in-play markets."""
        return [md for md in self._markets.values() if md.in_play]

    def is_stale(self, max_age: float = 60.0) -> bool:
        """Check if feed is stale (no data received recently)."""
        if self._disabled:
            return True
        if self._last_msg_at == 0:
            return True
        return (time.time() - self._last_msg_at) > max_age

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass
        logger.info("[BETFAIR] Feed stopped")
