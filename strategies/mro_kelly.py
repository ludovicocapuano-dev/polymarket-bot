"""
Strategia: MRO-Kelly — Mean Reversion Oscillator + Kelly Sizing
================================================================

Mercati BTC Up/Down 5-min su Polymarket con entry selettiva.
Differenza chiave vs crypto_5min (ELIMINATO v7.0): SOLO entry su
segnali MRO estremi (< -70 oversold, > +70 overbought) con filtri
di conferma (volume spike, price bounce, EMA trend, RSI/MACD).

MRO Oscillator = price_change_pct × 100 + volume_change_pct / 2
  Confronta candela corrente vs 5 candele fa (25 min).

Fase test: $10-20/trade, max 3 posizioni aperte.
Se WR > 58% dopo 50 trade, scale up.

Riferimenti:
  Kelly (1956) — Optimal bet sizing
  Wilder (1978) — RSI
  Appel (1979) — MACD
"""

import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field

import requests

from utils.polymarket_api import Market, PolymarketAPI
from utils.binance_feed import BinanceFeed
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "mro_kelly"

# ── Multi-crypto support (v2.0) ──
SUPPORTED_CRYPTOS = ["btc", "eth", "sol", "xrp"]

# ── Gamma API ──
GAMMA_API = "https://gamma-api.polymarket.com/markets"


# ══════════════════════════════════════════════════════════════
#  Candle aggregator — builds 5-min candles from tick data
# ══════════════════════════════════════════════════════════════

@dataclass
class Candle:
    """One 5-min OHLCV candle."""
    timestamp: float   # epoch start of candle
    open: float
    high: float
    low: float
    close: float
    volume: float      # dollar volume (price * qty)
    tick_count: int


@dataclass
class MROCalculator:
    """
    Maintains 5-candle history and computes:
      - MRO oscillator
      - 50 EMA (on closes)
      - RSI(14)
      - MACD (12/26/9)
    """
    candle_period: int = 300        # 5 minutes in seconds
    history_size: int = 60          # keep up to 60 candles (5h)
    ema_period: int = 50
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # ── State ──
    _candles: deque = field(default_factory=lambda: deque(maxlen=60))
    _current_candle: Candle = None
    _ema50: float = 0.0
    _ema12: float = 0.0
    _ema26: float = 0.0
    _ema_signal: float = 0.0
    _rsi: float = 50.0
    _avg_gain: float = 0.0
    _avg_loss: float = 0.0
    _ema_initialized: bool = False
    _rsi_initialized: bool = False
    _macd_initialized: bool = False
    _last_update: float = 0.0

    def update(self, binance_symbol_data) -> None:
        """
        Feed tick data from BinanceFeed's SymbolData.
        Aggregates into 5-min candles and updates indicators.
        """
        now = time.time()
        sd = binance_symbol_data
        if sd.price <= 0:
            return

        # Determine current candle epoch
        candle_epoch = int(now) - (int(now) % self.candle_period)

        if self._current_candle is None:
            # Start first candle
            self._current_candle = Candle(
                timestamp=candle_epoch,
                open=sd.price, high=sd.price,
                low=sd.price, close=sd.price,
                volume=0.0, tick_count=0,
            )

        # If we moved to a new candle period, finalize previous
        if candle_epoch > self._current_candle.timestamp:
            self._finalize_candle()
            self._current_candle = Candle(
                timestamp=candle_epoch,
                open=sd.price, high=sd.price,
                low=sd.price, close=sd.price,
                volume=0.0, tick_count=0,
            )

        # Update current candle from recent ticks
        c = self._current_candle
        c.close = sd.price
        c.high = max(c.high, sd.price)
        c.low = min(c.low, sd.price)

        # Accumulate volume from trade_flow since candle start
        candle_start = c.timestamp
        vol = 0.0
        for ts, price, qty, _ in sd.trade_flow:
            if ts >= candle_start:
                vol += price * qty
        c.volume = vol
        c.tick_count += 1

        self._last_update = now

    def _finalize_candle(self):
        """Close current candle, append to history, update indicators."""
        c = self._current_candle
        if c is None:
            return

        self._candles.append(c)

        # Update EMA-50
        close = c.close
        if not self._ema_initialized and len(self._candles) >= self.ema_period:
            # Seed with SMA
            closes = [cn.close for cn in self._candles]
            self._ema50 = sum(closes[-self.ema_period:]) / self.ema_period
            self._ema_initialized = True
        elif self._ema_initialized:
            k = 2.0 / (self.ema_period + 1)
            self._ema50 = close * k + self._ema50 * (1 - k)

        # Update MACD EMAs
        if len(self._candles) >= self.macd_slow:
            if not self._macd_initialized:
                closes = [cn.close for cn in self._candles]
                self._ema12 = sum(closes[-self.macd_fast:]) / self.macd_fast
                self._ema26 = sum(closes[-self.macd_slow:]) / self.macd_slow
                self._ema_signal = self._ema12 - self._ema26
                self._macd_initialized = True
            else:
                k12 = 2.0 / (self.macd_fast + 1)
                k26 = 2.0 / (self.macd_slow + 1)
                self._ema12 = close * k12 + self._ema12 * (1 - k12)
                self._ema26 = close * k26 + self._ema26 * (1 - k26)
                macd_line = self._ema12 - self._ema26
                ks = 2.0 / (self.macd_signal + 1)
                self._ema_signal = macd_line * ks + self._ema_signal * (1 - ks)

        # Update RSI
        if len(self._candles) >= 2:
            prev = self._candles[-2].close
            change = close - prev
            gain = max(change, 0.0)
            loss = max(-change, 0.0)

            if not self._rsi_initialized and len(self._candles) >= self.rsi_period + 1:
                # Seed RSI with SMA of gains/losses
                gains, losses = [], []
                candles_list = list(self._candles)
                for i in range(1, self.rsi_period + 1):
                    d = candles_list[-i].close - candles_list[-i - 1].close
                    gains.append(max(d, 0.0))
                    losses.append(max(-d, 0.0))
                self._avg_gain = sum(gains) / self.rsi_period
                self._avg_loss = sum(losses) / self.rsi_period
                self._rsi_initialized = True
            elif self._rsi_initialized:
                n = self.rsi_period
                self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
                self._avg_loss = (self._avg_loss * (n - 1) + loss) / n

            if self._rsi_initialized:
                if self._avg_loss == 0:
                    self._rsi = 100.0
                else:
                    rs = self._avg_gain / self._avg_loss
                    self._rsi = 100.0 - (100.0 / (1.0 + rs))

    # ── Accessors ──

    @property
    def ready(self) -> bool:
        """Need at least 6 candles (current + 5 lookback for MRO)."""
        return len(self._candles) >= 6

    @property
    def candles(self) -> list[Candle]:
        return list(self._candles)

    @property
    def current_candle(self) -> Candle | None:
        return self._current_candle

    def mro(self) -> float | None:
        """
        MRO Oscillator = price_change_pct × 100 + volume_change_pct / 2
        Compares current candle to 5 candles ago (25 min).
        """
        if not self.ready or self._current_candle is None:
            return None

        now_candle = self._current_candle
        ref_candle = self._candles[-5]  # 5 candles ago

        if ref_candle.close <= 0 or ref_candle.volume <= 0:
            return None

        price_change_pct = ((now_candle.close - ref_candle.close) / ref_candle.close) * 100.0
        volume_change_pct = ((now_candle.volume - ref_candle.volume) / ref_candle.volume) * 100.0

        # MRO = price_change_pct + volume_change_pct / 2
        # Clamp to [-200, +200] to handle REST fallback volume anomalies
        raw_mro = price_change_pct + volume_change_pct / 2.0
        return max(-200.0, min(200.0, raw_mro))

    def volume_change_pct(self) -> float | None:
        """Volume change % vs 5 candles ago."""
        if not self.ready or self._current_candle is None:
            return None
        ref = self._candles[-5]
        if ref.volume <= 0:
            return None
        return ((self._current_candle.volume - ref.volume) / ref.volume) * 100.0

    def price_bounced_from_low(self, threshold_pct: float = 0.1) -> bool:
        """Price has bounced at least threshold_pct% from candle low."""
        c = self._current_candle
        if c is None or c.low <= 0:
            return False
        bounce = ((c.close - c.low) / c.low) * 100.0
        return bounce >= threshold_pct

    def price_pulled_from_high(self, threshold_pct: float = 0.1) -> bool:
        """Price has pulled at least threshold_pct% from candle high."""
        c = self._current_candle
        if c is None or c.high <= 0:
            return False
        pull = ((c.high - c.close) / c.high) * 100.0
        return pull >= threshold_pct

    @property
    def ema50(self) -> float:
        return self._ema50

    @property
    def ema50_uptrend(self) -> bool:
        """Price above EMA50."""
        if not self._ema_initialized or self._current_candle is None:
            return False
        return self._current_candle.close > self._ema50

    @property
    def ema50_downtrend(self) -> bool:
        """Price below EMA50."""
        if not self._ema_initialized or self._current_candle is None:
            return False
        return self._current_candle.close < self._ema50

    @property
    def rsi(self) -> float:
        return self._rsi

    @property
    def macd(self) -> float:
        """MACD histogram (line - signal)."""
        if not self._macd_initialized:
            return 0.0
        return (self._ema12 - self._ema26) - self._ema_signal

    @property
    def macd_line(self) -> float:
        if not self._macd_initialized:
            return 0.0
        return self._ema12 - self._ema26


# ══════════════════════════════════════════════════════════════
#  MRO-Kelly Signal
# ══════════════════════════════════════════════════════════════

@dataclass
class MROSignal:
    """A trade signal from the MRO-Kelly strategy."""
    market: Market
    direction: str          # "UP" or "DOWN"
    side: str               # "YES" or "NO"
    mro_value: float
    probability: float      # Pr(Up) from logistic model
    market_price: float     # Polymarket price for our side
    edge: float             # Pr - market_price
    kelly_fraction: float   # f* quarter-Kelly
    target_size: float      # $ to bet
    btc_price: float
    rsi: float
    macd: float
    volume_change: float
    reasoning: str


# ══════════════════════════════════════════════════════════════
#  MRO-Kelly Strategy
# ══════════════════════════════════════════════════════════════

@dataclass
class MROKellyStrategy:
    """
    MRO-Kelly: Mean Reversion Oscillator on 5-min BTC markets.

    Selective entry only on strong MRO signals (|MRO| > 70) with
    confirmation filters. Quarter-Kelly sizing, small test bets.
    """

    api: PolymarketAPI
    risk: RiskManager
    binance: BinanceFeed
    horizon: object = None  # v13.1: HorizonClient for primary execution

    # ── Parameters ──
    mro_threshold: float = 70.0     # |MRO| must exceed this
    min_edge: float = 0.06          # 6% minimum edge
    min_bet: float = 5.0            # $ minimum bet
    max_bet: float = 20.0           # $ maximum bet (test phase)
    kelly_fraction: float = 0.25    # quarter-Kelly
    max_open_positions: int = 3     # max concurrent positions
    daily_stop_loss_pct: float = 0.10  # -10% daily stop
    cooldown_after_losses: float = 3600.0  # 1h pause after 3 consecutive losses
    cooldown_per_market: float = 60.0   # 1 trade per market per 60s

    # ── Volume spike thresholds ──
    vol_spike_up: float = 20.0      # +20% volume for UP entry
    vol_spike_down: float = 30.0    # +30% volume for DOWN entry

    # ── Multi-crypto ──
    max_positions_per_crypto: int = 2  # max 2 positions per crypto (8 total)

    # ── State ──
    _calculators: dict = field(default_factory=dict)  # symbol -> MROCalculator
    calculator: MROCalculator = field(default_factory=MROCalculator)  # backward compat (btc)
    _trades_executed: int = 0
    _daily_pnl: float = 0.0
    _daily_pnl_reset: float = 0.0   # epoch of last daily reset
    _consecutive_losses: int = 0
    _loss_cooldown_until: float = 0.0
    _open_position_count: int = 0
    _recently_traded: dict = field(default_factory=dict)   # market_id -> epoch
    _market_cache: dict = field(default_factory=dict)       # slug -> (Market, expire)
    _trade_log: deque = field(default_factory=lambda: deque(maxlen=200))
    _halted: bool = False
    _halt_reason: str = ""

    SLOT_DURATION = 300  # 5 minutes

    def __post_init__(self):
        self._daily_pnl_reset = time.time()
        # Initialize one MROCalculator per crypto
        for sym in SUPPORTED_CRYPTOS:
            self._calculators[sym] = MROCalculator()
        # backward compat: self.calculator points to btc
        self.calculator = self._calculators["btc"]

    # ══════════════════════════════════════════════════════════════
    #  Market Discovery — find crypto 5-min Up/Down markets
    # ══════════════════════════════════════════════════════════════

    def _discover_crypto_markets(self, symbol: str = "btc") -> list[Market]:
        """
        Find active crypto Up/Down 5-min markets on Polymarket.
        Uses the slug pattern: {symbol}-updown-5m-{epoch}.
        """
        now = int(time.time())
        current_slot = now - (now % self.SLOT_DURATION)
        markets = []
        sym_upper = symbol.upper()

        for offset in range(3):  # current + next 2 slots
            epoch = current_slot + offset * self.SLOT_DURATION
            slug = f"{symbol}-updown-5m-{epoch}"

            # Check cache
            cached = self._market_cache.get(slug)
            if cached:
                mkt, expire_ts = cached
                if time.time() < expire_ts:
                    markets.append(mkt)
                    continue
                else:
                    del self._market_cache[slug]

            try:
                resp = requests.get(
                    GAMMA_API,
                    params={"slug": slug},
                    timeout=5,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if not data:
                    continue

                item = data[0] if isinstance(data, list) else data
                condition_id = item.get("conditionId", item.get("condition_id", ""))
                question = item.get("question", "")

                outcomes = item.get("outcomes", ["Up", "Down"])
                if isinstance(outcomes, str):
                    import json as _json
                    try:
                        outcomes = _json.loads(outcomes)
                    except Exception:
                        outcomes = ["Up", "Down"]

                clob_token_ids = item.get("clobTokenIds", item.get("clob_token_ids", []))
                if isinstance(clob_token_ids, str):
                    import json as _json
                    try:
                        clob_token_ids = _json.loads(clob_token_ids)
                    except Exception:
                        clob_token_ids = []

                outcome_prices = item.get("outcomePrices", item.get("outcome_prices", ""))
                if isinstance(outcome_prices, str):
                    import json as _json
                    try:
                        outcome_prices = _json.loads(outcome_prices)
                    except Exception:
                        outcome_prices = []

                tokens = {}
                prices = {}
                if len(clob_token_ids) >= 2:
                    tokens["yes"] = clob_token_ids[0]
                    tokens["no"] = clob_token_ids[1]
                if len(outcome_prices) >= 2:
                    try:
                        prices["yes"] = float(outcome_prices[0])
                        prices["no"] = float(outcome_prices[1])
                    except (ValueError, TypeError):
                        prices = {"yes": 0.5, "no": 0.5}

                mkt = Market(
                    id=item.get("id", condition_id),
                    condition_id=condition_id,
                    question=question,
                    slug=slug,
                    tokens=tokens,
                    prices=prices,
                    volume=float(item.get("volume", 0)),
                    liquidity=float(item.get("liquidity", 0)),
                    end_date=item.get("endDate", item.get("end_date", "")),
                    active=item.get("active", True),
                    outcomes=outcomes,
                )
                self._market_cache[slug] = (mkt, time.time() + 60)
                markets.append(mkt)

            except Exception as e:
                logger.debug(f"[MRO-{sym_upper}] Discovery error for {slug}: {e}")
                continue

        return markets

    # ══════════════════════════════════════════════════════════════
    #  Crypto fee model (same as btc_latency)
    # ══════════════════════════════════════════════════════════════

    def _crypto_fee(self, price: float) -> float:
        """Polymarket dynamic crypto fee: price * 0.25 * (price*(1-price))^2"""
        return price * 0.25 * (price * (1.0 - price)) ** 2

    # ══════════════════════════════════════════════════════════════
    #  Probability model
    # ══════════════════════════════════════════════════════════════

    def _calc_probability(self, mro: float, odds_delta: float) -> float:
        """
        Pr(Up) = 1 / (1 + exp(-(MRO/100 + 0.5 * odds_delta)))

        MRO < -70 → oversold → Pr(Up) elevated (mean reversion UP)
        MRO > +70 → overbought → Pr(Up) depressed (mean reversion DOWN)

        Note: We negate MRO in the exponent because oversold (negative MRO)
        should give HIGH Pr(Up) for mean reversion.
        """
        # For mean reversion: oversold (MRO<0) → expect UP → negate MRO sign
        x = -mro / 100.0 + 0.5 * odds_delta
        return 1.0 / (1.0 + math.exp(-x))

    # ══════════════════════════════════════════════════════════════
    #  Daily PnL tracking
    # ══════════════════════════════════════════════════════════════

    def _check_daily_reset(self):
        """Reset daily PnL counter every 24h."""
        now = time.time()
        if now - self._daily_pnl_reset > 86400:
            self._daily_pnl = 0.0
            self._daily_pnl_reset = now
            self._halted = False
            self._halt_reason = ""
            logger.info("[MRO-KELLY] Daily PnL reset")

    def _is_halted(self) -> bool:
        """Check all halt conditions."""
        self._check_daily_reset()
        now = time.time()

        # Daily stop-loss
        if self.max_bet > 0:
            daily_limit = -self.max_bet * 10 * self.daily_stop_loss_pct
            # Use a more intuitive approach: -10% of bankroll
            bankroll = self.max_bet * 10  # ~$200 test bankroll
            daily_limit = -bankroll * self.daily_stop_loss_pct
            if self._daily_pnl <= daily_limit:
                if not self._halted:
                    logger.warning(
                        f"[MRO-KELLY] DAILY STOP HIT: PnL=${self._daily_pnl:.2f} "
                        f"<= limit=${daily_limit:.2f}"
                    )
                self._halted = True
                self._halt_reason = f"daily_stop: ${self._daily_pnl:.2f}"
                return True

        # Consecutive loss cooldown
        if now < self._loss_cooldown_until:
            return True

        return self._halted

    # ══════════════════════════════════════════════════════════════
    #  Scan — find MRO opportunities
    # ══════════════════════════════════════════════════════════════

    def scan(self, shared_markets: list[Market] | None = None) -> list[MROSignal]:
        """
        v2.0 Multi-crypto scan:
        1. Update MRO calculator per crypto from Binance tick data
        2. Check MRO thresholds per crypto
        3. Check entry filters (volume, price bounce, EMA, RSI/MACD)
        4. Find matching Polymarket 5-min markets per crypto
        5. Calculate edge (Pr vs market price)
        6. Return opportunities with edge > 6%
        """
        # Check halts
        if self._is_halted():
            return []

        if not hasattr(self, '_mro_log_counter'):
            self._mro_log_counter = 0
        self._mro_log_counter += 1

        now = time.time()
        all_signals = []

        # Track positions per crypto for the max_positions_per_crypto limit
        if not hasattr(self, '_positions_per_crypto'):
            self._positions_per_crypto = {sym: 0 for sym in SUPPORTED_CRYPTOS}

        for crypto_symbol in SUPPORTED_CRYPTOS:
            sym_upper = crypto_symbol.upper()
            log_prefix = f"MRO-{sym_upper}"

            # Need Binance feed for this crypto
            crypto_data = self.binance.get_symbol(crypto_symbol)
            if crypto_data.price <= 0:
                continue

            # Get calculator for this crypto
            calc = self._calculators.get(crypto_symbol)
            if calc is None:
                continue

            # Update calculator with fresh tick data
            calc.update(crypto_data)

            # Need enough candles for MRO calculation
            if not calc.ready:
                if self._mro_log_counter % 30 == 0:
                    logger.info(f"[{log_prefix}] Not ready: {len(calc.candles)} candles (need 6+)")
                continue

            mro_value = calc.mro()
            if mro_value is None:
                continue

            # ── Check MRO threshold ──
            if self._mro_log_counter % 30 == 0 or abs(mro_value) >= self.mro_threshold * 0.7:
                logger.info(
                    f"[{log_prefix}] MRO={mro_value:+.1f} (threshold=+/-{self.mro_threshold}) "
                    f"{sym_upper}=${crypto_data.price:,.2f} candles={len(calc.candles)}"
                )

            if abs(mro_value) < self.mro_threshold:
                continue

            vol_change = calc.volume_change_pct()
            if vol_change is None:
                continue

            signals = []

            # ── Determine direction ──
            if mro_value < -self.mro_threshold:
                # OVERSOLD -> expect mean reversion UP
                direction = "UP"

                if vol_change < self.vol_spike_up:
                    logger.debug(f"[{log_prefix}] SKIP UP: vol_change={vol_change:.1f}% < {self.vol_spike_up}%")
                    continue

                if not calc.price_bounced_from_low(0.1):
                    logger.debug(f"[{log_prefix}] SKIP UP: no price bounce from low")
                    continue

                if not calc.ema50_uptrend:
                    logger.debug(f"[{log_prefix}] SKIP UP: EMA50 downtrend")
                    continue

                rsi_ok = calc.rsi > 65
                macd_ok = calc.macd > 0
                if not (rsi_ok or macd_ok):
                    logger.debug(f"[{log_prefix}] SKIP UP: RSI={calc.rsi:.1f} MACD={calc.macd:.4f}")
                    continue

            elif mro_value > self.mro_threshold:
                # OVERBOUGHT -> expect mean reversion DOWN
                direction = "DOWN"

                if vol_change < self.vol_spike_down:
                    logger.debug(f"[{log_prefix}] SKIP DOWN: vol_change={vol_change:.1f}% < {self.vol_spike_down}%")
                    continue

                if not calc.price_pulled_from_high(0.1):
                    logger.debug(f"[{log_prefix}] SKIP DOWN: no price pull from high")
                    continue

                if not calc.ema50_downtrend:
                    logger.debug(f"[{log_prefix}] SKIP DOWN: EMA50 uptrend")
                    continue

                rsi_ok = calc.rsi < 35
                macd_ok = calc.macd < 0
                if not (rsi_ok or macd_ok):
                    logger.debug(f"[{log_prefix}] SKIP DOWN: RSI={calc.rsi:.1f} MACD={calc.macd:.4f}")
                    continue
            else:
                continue

            # ── Find matching Polymarket 5-min markets ──
            crypto_markets = self._discover_crypto_markets(symbol=crypto_symbol)
            if not crypto_markets:
                logger.debug(f"[{log_prefix}] No active {sym_upper} 5-min markets found")
                continue

            # ── Evaluate each market ──
            for m in crypto_markets:
                # Cooldown per market
                if now - self._recently_traded.get(m.id, 0) < self.cooldown_per_market:
                    continue

                # Max open positions (global)
                if self._open_position_count >= self.max_open_positions * len(SUPPORTED_CRYPTOS):
                    break

                # Max positions per crypto
                if self._positions_per_crypto.get(crypto_symbol, 0) >= self.max_positions_per_crypto:
                    break

                # Determine slot timing
                slug = getattr(m, "slug", "")
                try:
                    slot_epoch = int(slug.split("-")[-1])
                except (ValueError, IndexError):
                    continue

                t_remaining = (slot_epoch + self.SLOT_DURATION) - now
                if t_remaining <= 0 or t_remaining > self.SLOT_DURATION:
                    continue

                # Get market prices
                price_yes = m.prices.get("yes", 0.5)
                price_no = m.prices.get("no", 0.5)

                # odds_delta: how far market price deviates from 0.50
                odds_delta = price_yes - 0.50

                # Calculate probability using MRO model
                pr_up = self._calc_probability(mro_value, odds_delta)

                # Choose side based on direction
                if direction == "UP":
                    side = "YES"
                    our_prob = pr_up
                    market_price = price_yes
                else:
                    side = "NO"
                    our_prob = 1.0 - pr_up
                    market_price = price_no

                # Calculate edge
                fee = self._crypto_fee(market_price)
                edge = our_prob - market_price - fee

                if edge < self.min_edge:
                    logger.debug(f"[{log_prefix}] SKIP {m.slug}: edge={edge:.4f} < {self.min_edge}")
                    continue

                # ── Kelly sizing: f* = (p - m) / (1 - m) x 0.25 ──
                if market_price >= 1.0:
                    continue
                kelly_full = (our_prob - market_price) / (1.0 - market_price)
                kelly_quarter = kelly_full * self.kelly_fraction
                kelly_quarter = max(0.0, kelly_quarter)

                # Size: quarter-Kelly of test bankroll, clamped to [min_bet, max_bet]
                bankroll = self.max_bet * 10  # ~$200 test bankroll
                target_size = bankroll * kelly_quarter
                target_size = max(self.min_bet, min(self.max_bet, target_size))

                signal = MROSignal(
                    market=m,
                    direction=direction,
                    side=side,
                    mro_value=mro_value,
                    probability=our_prob,
                    market_price=market_price,
                    edge=edge,
                    kelly_fraction=kelly_quarter,
                    target_size=target_size,
                    btc_price=crypto_data.price,
                    rsi=calc.rsi,
                    macd=calc.macd,
                    volume_change=vol_change,
                    reasoning=(
                        f"[{sym_upper}] MRO={mro_value:+.1f} -> {direction} | "
                        f"{sym_upper}=${crypto_data.price:,.2f} | "
                        f"Pr={our_prob:.3f} vs mkt={market_price:.3f} "
                        f"edge={edge:.4f} fee={fee:.5f} | "
                        f"RSI={calc.rsi:.1f} MACD={calc.macd:.4f} | "
                        f"vol_chg={vol_change:+.1f}% EMA50={'UP' if calc.ema50_uptrend else 'DOWN'} | "
                        f"Kelly={kelly_quarter:.4f} size=${target_size:.0f} | "
                        f"tau={t_remaining:.0f}s"
                    ),
                )
                signals.append(signal)

            if signals:
                logger.info(
                    f"[{log_prefix}] {len(signals)} signal(s): MRO={mro_value:+.1f} "
                    f"dir={direction} RSI={calc.rsi:.1f}"
                )

            all_signals.extend(signals)

        return all_signals

    # ══════════════════════════════════════════════════════════════
    #  Execute
    # ══════════════════════════════════════════════════════════════

    async def execute(self, signal: MROSignal, paper: bool = True) -> bool:
        """Execute an MRO-Kelly trade."""
        now = time.time()

        # Detect crypto symbol from market slug
        slug = getattr(signal.market, "slug", "")
        crypto_symbol = slug.split("-")[0] if slug else "btc"
        sym_upper = crypto_symbol.upper()

        # Re-check halts
        if self._is_halted():
            return False

        # Cooldown
        if now - self._recently_traded.get(signal.market.id, 0) < self.cooldown_per_market:
            return False

        # Max open positions (global: max_open_positions * num_cryptos)
        total_max = self.max_open_positions * len(SUPPORTED_CRYPTOS)
        if self._open_position_count >= total_max:
            logger.info(
                f"[MRO-{sym_upper}] Max total positions reached ({self._open_position_count}/{total_max})"
            )
            return False

        # Max positions per crypto
        if not hasattr(self, '_positions_per_crypto'):
            self._positions_per_crypto = {sym: 0 for sym in SUPPORTED_CRYPTOS}
        if self._positions_per_crypto.get(crypto_symbol, 0) >= self.max_positions_per_crypto:
            logger.info(
                f"[MRO-{sym_upper}] Max per-crypto positions reached "
                f"({self._positions_per_crypto[crypto_symbol]}/{self.max_positions_per_crypto})"
            )
            return False

        token_key = signal.side.lower()
        token_id = signal.market.tokens.get(token_key)
        if not token_id:
            logger.warning(f"[MRO-{sym_upper}] No token ID for {token_key}")
            return False

        buy_price = signal.market_price
        size = signal.target_size

        # Risk manager check
        allowed, reason = self.risk.can_trade(
            STRATEGY_NAME, size, price=buy_price,
            side=f"BUY_{signal.side}", market_id=signal.market.id,
        )
        if not allowed:
            logger.info(f"[MRO-{sym_upper}] Risk block: {reason}")
            return False

        trade = Trade(
            timestamp=now,
            strategy=STRATEGY_NAME,
            market_id=signal.market.id,
            token_id=token_id,
            side=f"BUY_{signal.side}",
            size=size,
            price=buy_price,
            edge=signal.edge,
            reason=signal.reasoning,
        )

        if paper:
            # ── Paper simulation ──
            # Mean reversion model: use our probability estimate with noise
            model_accuracy = 0.65  # conservative — model is not perfect
            sim_prob = signal.probability * model_accuracy + 0.5 * (1 - model_accuracy)
            won = random.random() < sim_prob

            # Slippage: ~1% on crypto markets
            slippage = 0.99
            if won:
                pnl = size * ((1.0 / buy_price) - 1.0) * slippage
            else:
                pnl = -size * slippage

            # Fee
            fee = size * self._crypto_fee(buy_price)
            pnl -= fee

            logger.info(
                f"[PAPER] MRO-{sym_upper}: {signal.direction} "
                f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                f"MRO={signal.mro_value:+.1f} Pr={signal.probability:.3f} "
                f"edge={signal.edge:.4f} Kelly={signal.kelly_fraction:.4f} | "
                f"{'WIN' if won else 'LOSS'} pnl=${pnl:+.2f} (fee=${fee:.2f}) | "
                f"{sym_upper}=${signal.btc_price:,.2f} RSI={signal.rsi:.1f}"
            )

            self.risk.open_trade(trade)
            self.risk.close_trade(token_id, won=won, pnl=pnl)

            # Track PnL and consecutive losses
            self._daily_pnl += pnl
            if won:
                self._consecutive_losses = 0
            else:
                self._consecutive_losses += 1
                if self._consecutive_losses >= 3:
                    self._loss_cooldown_until = now + self.cooldown_after_losses
                    logger.warning(
                        f"[MRO-{sym_upper}] 3 consecutive losses — "
                        f"pausing for {self.cooldown_after_losses / 60:.0f}min"
                    )

            self._trade_log.append({
                "time": now,
                "direction": signal.direction,
                "side": signal.side,
                "price": buy_price,
                "size": size,
                "mro": signal.mro_value,
                "probability": signal.probability,
                "edge": signal.edge,
                "kelly": signal.kelly_fraction,
                "rsi": signal.rsi,
                "macd": signal.macd,
                "vol_change": signal.volume_change,
                "btc_price": signal.btc_price,
                "won": won,
                "pnl": pnl,
            })
        else:
            # v13.1: Horizon SDK primary execution
            target = min(buy_price + 0.03, 0.85)
            if self.horizon is not None:
                hz_result = self.horizon.execute_trade(
                    token_id=token_id,
                    side=f"BUY_{signal.side}",
                    size=size,
                    price=target,
                    strategy="mro_kelly",
                )
                if hz_result.success:
                    if hz_result.fill_price > 0:
                        trade.price = hz_result.fill_price
                    self.risk.open_trade(trade)
                    self._open_position_count += 1
                    self._positions_per_crypto[crypto_symbol] = self._positions_per_crypto.get(crypto_symbol, 0) + 1
                    logger.info(
                        f"[LIVE] MRO-{sym_upper} [{hz_result.engine.upper()}]: {signal.direction} "
                        f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                        f"MRO={signal.mro_value:+.1f} edge={signal.edge:.4f} | "
                        f"{sym_upper}=${signal.btc_price:,.2f}"
                    )
                else:
                    logger.warning(f"[MRO-{sym_upper}] Execution failed: {hz_result.error}")
                    return False
            else:
                # Legacy path: direct native smart_buy
                result = self.api.smart_buy(
                    token_id, size,
                    target_price=target,
                )
                if result:
                    if isinstance(result, dict) and result.get("_fill_price"):
                        trade.price = result["_fill_price"]
                    self.risk.open_trade(trade)
                    self._open_position_count += 1
                    self._positions_per_crypto[crypto_symbol] = self._positions_per_crypto.get(crypto_symbol, 0) + 1
                    logger.info(
                        f"[LIVE] MRO-{sym_upper} [NATIVE]: {signal.direction} "
                        f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                        f"MRO={signal.mro_value:+.1f} edge={signal.edge:.4f} | "
                        f"{sym_upper}=${signal.btc_price:,.2f}"
                    )
                else:
                    logger.warning(f"[MRO-{sym_upper}] Order failed: {signal.market.id}")
                    return False

        self._recently_traded[signal.market.id] = now
        self._trades_executed += 1
        return True

    # ══════════════════════════════════════════════════════════════
    #  Stats
    # ══════════════════════════════════════════════════════════════

    @property
    def stats(self) -> dict:
        """Statistics for dashboard and analysis."""
        trades = list(self._trade_log)
        wins = sum(1 for t in trades if t.get("won"))
        losses = len(trades) - wins
        wr = (wins / len(trades) * 100) if trades else 0
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        avg_edge = (
            sum(t.get("edge", 0) for t in trades) / len(trades)
        ) if trades else 0

        # Per-crypto calculator stats
        crypto_stats = {}
        for sym, calc in self._calculators.items():
            crypto_stats[sym] = {
                "ready": calc.ready,
                "candles": len(calc.candles),
                "mro": calc.mro(),
                "rsi": calc.rsi,
                "macd": calc.macd,
                "ema50": calc.ema50,
            }

        return {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": wr,
            "total_pnl": total_pnl,
            "daily_pnl": self._daily_pnl,
            "avg_edge": avg_edge,
            "consecutive_losses": self._consecutive_losses,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "mro_ready": self.calculator.ready,
            "candles": len(self.calculator.candles),
            "last_mro": self.calculator.mro(),
            "rsi": self.calculator.rsi,
            "macd": self.calculator.macd,
            "ema50": self.calculator.ema50,
            "scale_eligible": len(trades) >= 50 and wr >= 58,
            "crypto_stats": crypto_stats,
        }
