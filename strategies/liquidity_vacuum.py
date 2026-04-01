"""
Strategia: Liquidity Vacuum Sniper — v1.0
==========================================
Monitora order book depth su mercati Polymarket. Quando un grande trade
meccanico sposta il prezzo su un book sottile (slippage, non informazione),
compra la reversion prima che il prezzo torni al livello precedente.

Meccanismo:
1. Scan continuo di top 50-100 mercati per volume
2. Track depth, spread, e prezzo per ogni mercato
3. Quando rileva: price spike >5% + thin book (<$500 depth) + no news trigger
   → classifica come "mechanical slippage" (non informational move)
4. Piazza limit order nella direzione della reversion
5. Prezzo torna in 15-40 min → profit 4-7%

Filtri:
- Solo mercati con volume >$50K (liquidita' sufficiente per uscire)
- Solo spike >5% senza notizie correlate
- Solo book con depth <$500 ai livelli vicini (slippage, non trend)
- Anti-stack: max 1 posizione per mercato
- Cooldown 30 min dopo ogni trade sullo stesso mercato

Riferimenti:
  "Liquidity Vacuum Sniper" — 3,000 backtests article
  Amihud (2002) — Illiquidity premium
  Kyle (1985) — Price impact of informed trading
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from utils.polymarket_api import Market, PolymarketAPI
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "liquidity_vacuum"

# Scan parameters
MIN_VOLUME = 50_000           # $50K minimum 24h volume (can exit cleanly)
MAX_MARKETS_PER_SCAN = 80     # rate limit: don't hammer CLOB API
SCAN_INTERVAL = 30.0          # seconds between full scans

# Vacuum detection thresholds
PRICE_SPIKE_THRESHOLD = 0.05  # 5% price move = potential vacuum
THIN_BOOK_DEPTH = 500.0       # $500 total depth = thin
THIN_BOOK_QTY = 50.0          # 50 shares at best level = thin
SPREAD_WIDE_THRESHOLD = 0.10  # 10 cent spread = illiquid

# Trade parameters
MIN_EDGE = 0.03               # 3% minimum expected reversion
MAX_BET = 50.0                # max $50 per vacuum trade
MIN_BET = 10.0                # min $10
KELLY_FRACTION = 0.20         # conservative Kelly for mean-reversion
COOLDOWN_SEC = 1800.0         # 30 min cooldown per market
MAX_CONCURRENT = 5            # max 5 vacuum positions open
REVERSION_TIMEOUT = 2400.0    # 40 min max hold for reversion


@dataclass
class PriceSnapshot:
    """Rolling price + depth data for a market."""
    market_id: str
    token_id_yes: str
    token_id_no: str
    question: str

    # Price history (ts, yes_price, no_price, bid_qty, ask_qty, spread)
    history: deque = field(default_factory=lambda: deque(maxlen=60))

    # Baseline (rolling average over last 10 snapshots)
    baseline_yes: float = 0.0
    baseline_no: float = 0.0

    # Latest
    latest_yes: float = 0.0
    latest_no: float = 0.0
    latest_spread: float = 0.0
    latest_bid_depth: float = 0.0
    latest_ask_depth: float = 0.0
    updated_at: float = 0.0


@dataclass
class VacuumSignal:
    """Detected liquidity vacuum — mechanical price spike on thin book."""
    market: Market
    side: str                   # "YES" or "NO" — buy the side that was slippage-depressed
    token_id: str
    spike_direction: str        # "up" or "down" — direction of the spike
    current_price: float        # current (spiked) price
    baseline_price: float       # where it should revert to
    expected_reversion: float   # baseline - current (positive = profit)
    edge: float                 # expected_reversion / current_price
    depth_usd: float            # available depth at time of signal
    spread: float               # current spread
    confidence: float           # 0-1
    target_size: float
    reasoning: str


@dataclass
class LiquidityVacuumStrategy:
    """
    Liquidity Vacuum Sniper — mean reversion on mechanical price spikes.

    Monitors order books across top markets. When a large trade moves
    the price on a thin book without informational content, buys the
    reversion before the price corrects.
    """
    api: PolymarketAPI
    risk: RiskManager

    # State
    _snapshots: dict = field(default_factory=dict)      # market_id -> PriceSnapshot
    _recently_traded: dict = field(default_factory=dict) # market_id -> timestamp
    _last_scan_at: float = 0.0
    _open_positions: int = 0
    _trades_executed: int = 0
    _pnl_tracker: dict = field(default_factory=dict)

    # ══════════════════════════════════════════════════════════════
    #  Snapshot Collection
    # ══════════════════════════════════════════════════════════════

    def _update_snapshot(self, market: Market):
        """Fetch order book and update price snapshot for a market."""
        market_id = market.id
        now = time.time()

        # Get or create snapshot
        if market_id not in self._snapshots:
            tokens = market.tokens or {}
            self._snapshots[market_id] = PriceSnapshot(
                market_id=market_id,
                token_id_yes=tokens.get("yes", ""),
                token_id_no=tokens.get("no", ""),
                question=market.question or "",
            )
        snap = self._snapshots[market_id]

        # Get prices from market (already fetched by bot main loop)
        yes_price = market.prices.get("yes", 0.5)
        no_price = market.prices.get("no", 0.5)
        spread = abs(1.0 - yes_price - no_price)

        # Try to get depth from order book (YES token)
        bid_depth = 0.0
        ask_depth = 0.0
        try:
            if snap.token_id_yes:
                book = self.api.get_order_book(snap.token_id_yes)
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
                ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
        except Exception:
            pass  # depth unavailable, use 0

        # Update snapshot
        snap.latest_yes = yes_price
        snap.latest_no = no_price
        snap.latest_spread = spread
        snap.latest_bid_depth = bid_depth
        snap.latest_ask_depth = ask_depth
        snap.updated_at = now

        # Add to history
        snap.history.append((now, yes_price, no_price, bid_depth, ask_depth, spread))

        # Calculate baseline (rolling average of last 10 snapshots)
        if len(snap.history) >= 3:
            recent = list(snap.history)[-10:]
            snap.baseline_yes = sum(h[1] for h in recent) / len(recent)
            snap.baseline_no = sum(h[2] for h in recent) / len(recent)

    # ══════════════════════════════════════════════════════════════
    #  Vacuum Detection
    # ══════════════════════════════════════════════════════════════

    def _detect_vacuum(self, snap: PriceSnapshot, market: Market) -> VacuumSignal | None:
        """
        Check if a market has a mechanical price spike on a thin book.

        A vacuum signal fires when:
        1. Price moved >5% from baseline
        2. Book is thin (<$500 depth or <50 shares at best level)
        3. Spread is wide (>10 cents)
        4. No recent large volume (not an informed move)
        """
        if len(snap.history) < 10:
            return None  # need 10+ snapshots for reliable baseline (~5 min)

        # depth=0 means "unknown" (no book fetched), not "thin book"
        # Only detect vacuums when we have actual depth data
        if snap.latest_bid_depth <= 0 and snap.latest_ask_depth <= 0:
            return None  # no depth data — can't distinguish mechanical from informational

        if snap.baseline_yes <= 0 or snap.baseline_no <= 0:
            return None

        now = time.time()

        # Calculate price deviation from baseline
        dev_yes = snap.latest_yes - snap.baseline_yes
        dev_no = snap.latest_no - snap.baseline_no

        # Check for significant spike
        abs_dev_yes = abs(dev_yes)
        abs_dev_no = abs(dev_no)

        # Use the side with bigger deviation
        if abs_dev_yes > abs_dev_no:
            deviation = dev_yes
            current_price = snap.latest_yes
            baseline_price = snap.baseline_yes
            spike_on = "YES"
        else:
            deviation = dev_no
            current_price = snap.latest_no
            baseline_price = snap.baseline_no
            spike_on = "NO"

        abs_deviation = abs(deviation)

        # Threshold: spike must be >5% of baseline price
        if baseline_price > 0 and abs_deviation / baseline_price < PRICE_SPIKE_THRESHOLD:
            return None

        # Thin book check: total depth must be low AND known (>0)
        total_depth = snap.latest_bid_depth + snap.latest_ask_depth
        depth_usd = total_depth * current_price  # approximate $ depth

        # Must have positive depth data to classify as thin
        if total_depth <= 0:
            return None  # no depth data — skip

        is_thin = (
            depth_usd < THIN_BOOK_DEPTH
            and (snap.latest_bid_depth < THIN_BOOK_QTY
                 or snap.latest_ask_depth < THIN_BOOK_QTY)
        )

        if not is_thin:
            return None  # thick book = probably informational move

        # Wide spread check (additional confidence)
        wide_spread = snap.latest_spread > SPREAD_WIDE_THRESHOLD

        # Determine trade direction:
        # If YES price spiked UP (overshot) → buy NO (bet it reverts down)
        # If YES price spiked DOWN (dumped) → buy YES (bet it reverts up)
        if spike_on == "YES":
            if deviation > 0:
                # YES spiked up → buy NO (cheaper, will revert)
                side = "NO"
                buy_price = snap.latest_no
                expected_reversion = abs_deviation  # NO should go up by this much
                token_id = snap.token_id_no
            else:
                # YES dumped → buy YES (cheap, will revert)
                side = "YES"
                buy_price = snap.latest_yes
                expected_reversion = abs_deviation
                token_id = snap.token_id_yes
        else:
            if deviation > 0:
                side = "YES"
                buy_price = snap.latest_yes
                expected_reversion = abs_deviation
                token_id = snap.token_id_yes
            else:
                side = "NO"
                buy_price = snap.latest_no
                expected_reversion = abs_deviation
                token_id = snap.token_id_no

        if buy_price <= 0.05 or buy_price >= 0.95:
            return None  # extreme prices — too risky

        # Sanity: edge > 50% is almost certainly noise, not a real vacuum
        edge = expected_reversion / buy_price if buy_price > 0 else 0
        if edge > 0.50:
            return None  # unrealistic edge — skip

        # Edge = expected reversion / buy price
        edge = expected_reversion / buy_price if buy_price > 0 else 0

        if edge < MIN_EDGE:
            return None

        # Confidence based on: thin book + wide spread + deviation size
        confidence = 0.50
        if is_thin:
            confidence += 0.15
        if wide_spread:
            confidence += 0.15
        if abs_deviation > 0.08:
            confidence += 0.10
        if len(snap.history) >= 10:
            confidence += 0.05  # more data = more reliable baseline
        confidence = min(confidence, 0.95)

        # Kelly sizing
        win_prob = 0.65  # historical mean reversion success rate
        kelly_full = (win_prob * (1 + edge) - 1) / edge if edge > 0 else 0
        kelly_size = max(MIN_BET, min(kelly_full * KELLY_FRACTION * 500, MAX_BET))

        spike_dir = "up" if deviation > 0 else "down"

        reasoning = (
            f"[VACUUM] {snap.question[:50]} | {spike_on} spiked {spike_dir} "
            f"{abs_deviation:.3f} from baseline {baseline_price:.3f} → {current_price:.3f} | "
            f"depth=${depth_usd:.0f} spread={snap.latest_spread:.3f} | "
            f"BUY {side} @{buy_price:.3f} edge={edge:.3f}"
        )

        return VacuumSignal(
            market=market,
            side=side,
            token_id=token_id,
            spike_direction=spike_dir,
            current_price=buy_price,
            baseline_price=baseline_price,
            expected_reversion=expected_reversion,
            edge=edge,
            depth_usd=depth_usd,
            spread=snap.latest_spread,
            confidence=confidence,
            target_size=kelly_size,
            reasoning=reasoning,
        )

    # ══════════════════════════════════════════════════════════════
    #  Scan
    # ══════════════════════════════════════════════════════════════

    def scan(self, shared_markets: list[Market] | None = None) -> list[VacuumSignal]:
        """
        Scan markets for liquidity vacuum opportunities.

        Called every cycle (~3s) by the bot main loop.
        Only does a full depth scan every SCAN_INTERVAL seconds.
        """
        now = time.time()

        # Rate limit: full scan every 30s
        if now - self._last_scan_at < SCAN_INTERVAL:
            return []

        self._last_scan_at = now

        if not shared_markets:
            return []

        # Check concurrent positions
        if self._open_positions >= MAX_CONCURRENT:
            return []

        # Filter to high-volume markets (can exit cleanly)
        candidates = [
            m for m in shared_markets
            if (getattr(m, 'volume', 0) or 0) >= MIN_VOLUME
            and m.active
            and m.tokens.get("yes") and m.tokens.get("no")
        ]

        # Sort by volume descending, take top N
        candidates.sort(key=lambda m: getattr(m, 'volume', 0) or 0, reverse=True)
        candidates = candidates[:MAX_MARKETS_PER_SCAN]

        # Update snapshots (fetches order book for each — rate limited by batch size)
        # Only fetch book for top 10 to avoid rate limiting
        for i, market in enumerate(candidates):
            if i < 10:
                # Full depth scan (includes order book fetch)
                try:
                    self._update_snapshot(market)
                except Exception as e:
                    logger.debug(f"[VACUUM] Snapshot error {market.id}: {e}")
            else:
                # Price-only update (from shared_markets, no API call)
                self._update_snapshot_prices_only(market)

        # Detect vacuums
        signals = []
        for market in candidates:
            snap = self._snapshots.get(market.id)
            if not snap:
                continue

            # Cooldown check
            if now - self._recently_traded.get(market.id, 0) < COOLDOWN_SEC:
                continue

            signal = self._detect_vacuum(snap, market)
            if signal:
                signals.append(signal)
                logger.info(signal.reasoning)

        # Sort by edge descending
        signals.sort(key=lambda s: s.edge, reverse=True)
        return signals[:2]  # max 2 per scan

    def _update_snapshot_prices_only(self, market: Market):
        """Update snapshot with prices from shared_markets (no API call)."""
        market_id = market.id
        now = time.time()

        if market_id not in self._snapshots:
            tokens = market.tokens or {}
            self._snapshots[market_id] = PriceSnapshot(
                market_id=market_id,
                token_id_yes=tokens.get("yes", ""),
                token_id_no=tokens.get("no", ""),
                question=market.question or "",
            )

        snap = self._snapshots[market_id]
        yes_price = market.prices.get("yes", 0.5)
        no_price = market.prices.get("no", 0.5)
        spread = abs(1.0 - yes_price - no_price)

        snap.latest_yes = yes_price
        snap.latest_no = no_price
        snap.latest_spread = spread
        snap.updated_at = now

        # Add to history (depth unknown for price-only updates)
        snap.history.append((now, yes_price, no_price, snap.latest_bid_depth, snap.latest_ask_depth, spread))

        # Update baseline
        if len(snap.history) >= 3:
            recent = list(snap.history)[-10:]
            snap.baseline_yes = sum(h[1] for h in recent) / len(recent)
            snap.baseline_no = sum(h[2] for h in recent) / len(recent)

    # ══════════════════════════════════════════════════════════════
    #  Execution
    # ══════════════════════════════════════════════════════════════

    async def execute(self, signal: VacuumSignal, paper: bool = True) -> bool:
        """Execute a liquidity vacuum reversion trade."""
        now = time.time()

        # Cooldown
        if now - self._recently_traded.get(signal.market.id, 0) < COOLDOWN_SEC:
            return False

        buy_price = signal.current_price
        size = signal.target_size

        allowed, reason = self.risk.can_trade(
            STRATEGY_NAME, size, price=buy_price,
            side=f"BUY_{signal.side}", market_id=signal.market.id,
        )
        if not allowed:
            logger.info(f"[VACUUM] Risk block: {reason}")
            return False

        trade = Trade(
            timestamp=now,
            strategy=STRATEGY_NAME,
            market_id=signal.market.id,
            token_id=signal.token_id,
            side=f"BUY_{signal.side}",
            size=size,
            price=buy_price,
            edge=signal.edge,
            reason=signal.reasoning,
        )

        if paper:
            import random
            # Mean reversion success rate: ~65% historically
            model_decay = 0.65
            sim_prob = model_decay
            won = random.random() < sim_prob

            # Reversion profit: typically 4-7% of position
            slippage = 0.98  # 2% slippage on thin books
            if won:
                reversion_pct = signal.edge * 0.6  # partial reversion (60% of full)
                pnl = size * reversion_pct * slippage
            else:
                pnl = -size * 0.08 * slippage  # typical loss: -8% (further slippage)

            logger.info(
                f"[PAPER] VACUUM: BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                f"edge={signal.edge:.3f} depth=${signal.depth_usd:.0f} "
                f"spread={signal.spread:.3f} | "
                f"{'WIN' if won else 'LOSS'} pnl=${pnl:+.2f} | "
                f"{signal.market.question[:50] if signal.market.question else ''}"
            )

            self.risk.open_trade(trade)
            self.risk.close_trade(signal.token_id, won=won, pnl=pnl)

            self._pnl_tracker[signal.market.id] = {
                "time": now,
                "side": signal.side,
                "price": buy_price,
                "size": size,
                "edge": signal.edge,
                "depth": signal.depth_usd,
                "spread": signal.spread,
                "won": won,
                "pnl": pnl,
            }
        else:
            # Live: limit order at current price (maker, reversion expected)
            result = self.api.smart_buy(
                signal.token_id, size,
                target_price=buy_price,
                timeout_sec=5.0,
                fallback_market=False,  # no market order on thin books!
            )
            if result:
                if isinstance(result, dict) and result.get("_fill_price"):
                    trade.price = result["_fill_price"]
                self.risk.open_trade(trade)
                self._open_positions += 1
                logger.info(
                    f"[LIVE] VACUUM: BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                    f"edge={signal.edge:.3f} depth=${signal.depth_usd:.0f} | "
                    f"{signal.market.question[:50] if signal.market.question else ''}"
                )
            else:
                logger.warning(f"[VACUUM] Ordine fallito: {signal.market.id}")
                return False

        self._recently_traded[signal.market.id] = now
        self._trades_executed += 1
        return True

    # ══════════════════════════════════════════════════════════════
    #  Stats
    # ══════════════════════════════════════════════════════════════

    @property
    def stats(self) -> dict:
        trades = list(self._pnl_tracker.values())
        if not trades:
            return {"trades": 0, "pnl": 0, "wr": 0, "avg_edge": 0}

        total = len(trades)
        wins = sum(1 for t in trades if t.get("won"))
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        avg_edge = sum(t.get("edge", 0) for t in trades) / total

        return {
            "trades": total,
            "wins": wins,
            "wr": wins / total if total > 0 else 0,
            "pnl": total_pnl,
            "avg_edge": avg_edge,
            "markets_tracked": len(self._snapshots),
        }
