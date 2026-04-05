"""
Strategia: BTC Latency Arbitrage — v3.0 (Multi-Mode)
=====================================================

Tre modalità di trading complementari su mercati BTC Up/Down 5-min:

1. LATENCY MODE (v2.0): entry 30-240s nel slot
   - Black-Scholes fair value vs prezzo stale Polymarket
   - 4 filtri di conferma (vol_z, acceleration, TFI, OBI)
   - Richiede volatilità e volume surge

2. OFI MOMENTUM (v3.0): entry 30-240s nel slot
   - Order Flow Imbalance come trigger primario (non vol_z)
   - OFI ha correlazione lineare con price changes (Anastasopoulos 2025)
   - Entra come maker (limit order) per evitare taker fee 3.15%
   - Richiede OFI + buy_pressure allineati e persistenti (20s)

3. LAST-30s SNIPER (v3.0): entry ultimi 30s del slot
   - Outcome quasi-determinato (|d| > 3.0 = 99.9% certezza)
   - Book si svuota, prezzi stale (LP pullano quotes)
   - Fee minime a prezzi estremi ($0.95 → fee ~0.02%)
   - Win rate >95%, sizing aggressivo

Fee model (Polymarket dynamic crypto fees):
  fee = price * 0.25 * (price * (1 - price))^2
  - @$0.50: fee = 0.39% (massima)
  - @$0.80: fee = 0.08%
  - @$0.95: fee = 0.02% (quasi zero)

Riferimenti:
  Black, Scholes (1973); Andersen, Bollerslev (1998)
  Kyle (1985); Kelly (1956)
  Anastasopoulos, Gradojevic (2025) — Order Flow and Crypto Returns
  Easley, Lopez de Prado, O'Hara (2012) — VPIN
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

STRATEGY_NAME = "btc_latency"

# ── Multi-crypto support (v3.1) ──
SUPPORTED_CRYPTOS = ["btc", "eth", "sol", "xrp"]


def _norm_cdf(x: float) -> float:
    """Phi(x) = 0.5 * (1 + erf(x / sqrt(2)))  —  no scipy needed."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class LatencySignal:
    """Segnale con fair value da modello Black-Scholes."""
    market: Market
    direction: str          # "UP" o "DOWN"
    side: str               # "YES" o "NO"
    fair_prob: float        # P(Up) dal modello
    market_price: float     # prezzo Polymarket del side scelto
    edge: float             # edge netto (dopo fee)
    confidence: float       # 0-1, da filtri di conferma
    kelly_fraction: float   # f* half-Kelly
    target_size: float      # $ da investire
    btc_price: float
    btc_open: float         # prezzo apertura slot
    realized_vol: float     # sigma per-second
    d_score: float          # d nel modello BS
    vol_zscore: float       # volume anomaly Z-score
    filters_passed: int     # quanti filtri confermano (0-4)
    time_remaining: float   # secondi rimanenti nel slot
    reasoning: str


@dataclass
class BTCLatencyStrategy:
    """
    Latency arbitrage quantitativo su mercati Bitcoin Up/Down 5-min.
    Usa Black-Scholes per calcolare il fair value, confronta con prezzo
    Polymarket, e trada quando il gap supera la soglia minima.
    """

    api: PolymarketAPI
    risk: RiskManager
    binance: BinanceFeed
    horizon: object = None  # v13.1: HorizonClient for primary execution
    fast_executor: object = None  # v14.0: FastExecutor for sub-100ms execution

    # ── Parametri Latency Mode ──
    bankroll: float = 5000.0         # capitale dedicato a questa strategia
    base_size: float = 25.0          # v12.9: ridotto da $100 — $25 era profittevole (7W/1L +$80)
    max_size: float = 50.0           # v13.2: ripristinato $50 con capitale $2000
    min_edge: float = 0.05           # v13.3: alzato da 3% — 3.3% edge su $43 = $1.43 EV, troppo poco per latency
    min_vol_zscore: float = 1.0      # Z-score volume minimo (surge detection)
    min_filters: int = 2             # minimo filtri confermanti su 4
    max_entry_price: float = 0.80    # non comprare sopra
    min_entry_price: float = 0.52    # non comprare sotto (troppo vicino a 50/50)
    entry_window: tuple = (30, 240)  # secondi nel slot: entry tra 30s e 240s
    cooldown_sec: float = 60.0       # un solo trade per slot
    trade_hours: tuple = (0, 0, 23, 59)  # 24/7

    # ── Parametri OFI Momentum (v3.0) ──
    ofi_min_imbalance: float = 0.60   # OFI threshold (>0.60 = forte buy, <0.40 = forte sell)
    ofi_min_pressure: float = 0.58    # buy_pressure allineata
    ofi_min_edge: float = 0.02        # edge minimo (più basso — entro come maker)
    ofi_persistence_window: int = 20  # secondi di OFI persistente

    # ── Parametri Last-30s Sniper (v3.0) ──
    sniper_window: tuple = (270, 295)  # entry tra 270s e 295s (ultimi 30s, no ultimi 5s)
    sniper_min_d: float = 3.0         # |d| minimo (99.87% certezza)
    sniper_min_price: float = 0.88    # prezzo minimo (outcome quasi-certo)
    sniper_max_size: float = 50.0     # v12.9: ridotto da $200 — cap anche su alta certezza

    # ── State ──
    _trades_executed: int = 0
    _recently_traded: dict = field(default_factory=dict)
    _pnl_tracker: dict = field(default_factory=dict)
    _signal_history: deque = field(default_factory=lambda: deque(maxlen=500))
    _market_cache: dict = field(default_factory=dict)   # slug -> (Market, expire_ts)
    _slot_opens: dict = field(default_factory=dict)      # (symbol, epoch) -> price
    _ofi_history: deque = field(default_factory=lambda: deque(maxlen=100))  # v3.0: (ts, ofi, bp)

    # ── Constants ──
    GAMMA_API = "https://gamma-api.polymarket.com/markets"
    SLOT_DURATION = 300  # 5 minuti

    # ══════════════════════════════════════════════════════════════
    #  Market Discovery (unchanged from v1.0 — slug pattern works)
    # ══════════════════════════════════════════════════════════════

    def discover_crypto_markets(self, symbol: str = "btc") -> list[Market]:
        """
        Scopre mercati crypto Up/Down 5-min attivi usando il pattern slug.
        Slug: {symbol}-updown-5m-{epoch} dove epoch e' allineato a 300s.
        Fetcha il slot corrente e i prossimi 2 (15 min di copertura).
        """
        now = int(time.time())
        current_slot = now - (now % self.SLOT_DURATION)
        markets = []
        sym_upper = symbol.upper()

        for offset in range(3):
            epoch = current_slot + offset * self.SLOT_DURATION
            slug = f"{symbol}-updown-5m-{epoch}"

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
                    self.GAMMA_API,
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

                logger.debug(
                    f"[{sym_upper}-DISCOVERY] Found: {question} | "
                    f"prices={prices} slug={slug}"
                )
            except Exception as e:
                logger.debug(f"[{sym_upper}-DISCOVERY] Error fetching {slug}: {e}")
                continue

        if markets:
            logger.info(f"[{sym_upper}-DISCOVERY] {len(markets)} mercati attivi trovati")

        return markets

    # ══════════════════════════════════════════════════════════════
    #  Core: Scan con modello quantitativo
    # ══════════════════════════════════════════════════════════════

    def _crypto_fee(self, price: float) -> float:
        """Polymarket dynamic crypto fee: price * 0.25 * (price*(1-price))^2"""
        return price * 0.25 * (price * (1.0 - price)) ** 2

    def _ofi_is_persistent(self, direction: str, window: int = 20) -> bool:
        """
        v3.0: Controlla che l'OFI sia stato persistentemente nella stessa
        direzione per almeno `window` secondi. Filtra noise spikes.
        """
        if len(self._ofi_history) < 3:
            return False
        now = time.time()
        cutoff = now - window
        relevant = [(ts, ofi, bp) for ts, ofi, bp in self._ofi_history if ts >= cutoff]
        if len(relevant) < 3:
            return False

        if direction == "UP":
            return all(ofi > 0.55 and bp > 0.55 for _, ofi, bp in relevant)
        else:
            return all(ofi < -0.55 and bp < 0.45 for _, ofi, bp in relevant)

    def scan(self, shared_markets: list[Market] | None = None) -> list[LatencySignal]:
        """
        v3.1: Multi-crypto, multi-mode scan — Latency + OFI Momentum + Last-30s Sniper.

        Iterates over all supported cryptos (BTC, ETH, SOL, XRP).
        Tutti e tre calcolano fair value via Black-Scholes ma hanno trigger
        diversi e finestre temporali diverse. Le fee sono calcolate per ogni
        modalità in base al prezzo di entry.
        """
        now = time.time()

        # Cleanup slot opens vecchi — key is (symbol, epoch)
        stale = [k for k in self._slot_opens if now - k[1] > 600]
        for k in stale:
            del self._slot_opens[k]

        all_signals = []

        for crypto_symbol in SUPPORTED_CRYPTOS:
            sym_upper = crypto_symbol.upper()
            log_prefix = f"CRYPTO-LATENCY-{sym_upper}"

            if self.binance.symbol_price(crypto_symbol) == 0:
                continue

            crypto_markets = self.discover_crypto_markets(symbol=crypto_symbol)
            if not crypto_markets:
                continue

            sd = self.binance.get_symbol(crypto_symbol)

            # ── 1. Realized volatility (per-second) ──
            sigma = self._realized_vol(sd, window=60)
            if sigma < 1e-8:
                if int(now) % 60 < 8:
                    cutoff = now - 300
                    recent = [p for ts, p in sd.history if ts >= cutoff]
                    unique = len(set(recent)) if recent else 0
                    logger.info(
                        f"[{log_prefix}] sigma too low: {sigma:.10f} "
                        f"(ticks={len(sd.history)} unique_5m={unique})"
                    )
                continue

            # ── 2. Microstructure features ──
            vol_z = self._volume_zscore(sd, short_window=5, long_window=30)
            accel = self._price_acceleration(sd, symbol=crypto_symbol)
            buy_pressure = self._calc_buy_pressure(sd, window=10)
            obi = self._calc_obi(sd)

            # v3.0: Track OFI history per persistence check (shared across cryptos — OK for now)
            self._ofi_history.append((now, obi, buy_pressure))

            signals = []

            for m in crypto_markets:
                if now - self._recently_traded.get(m.id, 0) < self.cooldown_sec:
                    continue

                slug = getattr(m, "slug", "")
                try:
                    slot_epoch = int(slug.split("-")[-1])
                except (ValueError, IndexError):
                    continue

                t_elapsed = now - slot_epoch
                t_remaining = (slot_epoch + self.SLOT_DURATION) - now
                if t_remaining <= 0:
                    continue

                s_open = self._get_slot_open(slot_epoch, sd, symbol=sym_upper)
                if s_open is None or s_open <= 0:
                    continue

                s_now = sd.price

                # ═══ BLACK-SCHOLES ═══
                log_return = math.log(s_now / s_open)
                vol_term = sigma * math.sqrt(t_remaining)

                if vol_term < 1e-10:
                    fair_up = 1.0 if log_return > 0 else 0.0
                    d = 999.0 if log_return > 0 else -999.0
                else:
                    d = log_return / vol_term
                    fair_up = _norm_cdf(d)

                price_yes = m.prices.get("yes", 0.5)
                price_no = m.prices.get("no", 0.5)

                edge_up = fair_up - price_yes
                edge_down = (1.0 - fair_up) - price_no

                if edge_up >= edge_down:
                    direction, side = "UP", "YES"
                    edge_raw, buy_price, fair_p = edge_up, price_yes, fair_up
                else:
                    direction, side = "DOWN", "NO"
                    edge_raw, buy_price, fair_p = edge_down, price_no, 1.0 - fair_up

                fee = self._crypto_fee(buy_price)
                edge_net = edge_raw - fee

                # ════════════════════════════════════════════════════
                #  MODE 1: LAST-30s SNIPER
                #  Ultimi 30s del slot, outcome quasi-determinato.
                #  Fee minime a prezzi estremi. Win rate >95%.
                # ════════════════════════════════════════════════════
                if self.sniper_window[0] <= t_elapsed <= self.sniper_window[1]:
                    abs_d = abs(d)
                    if abs_d >= self.sniper_min_d and buy_price >= self.sniper_min_price:
                        sniper_fee = self._crypto_fee(buy_price)
                        sniper_edge = edge_raw - sniper_fee

                        if sniper_edge > 0.005:
                            kelly_full = max(0.0, (fair_p - buy_price) / (1.0 - buy_price))
                            size = min(self.bankroll * kelly_full * 0.7, self.sniper_max_size)
                            size = max(self.base_size, size)

                            sig = LatencySignal(
                                market=m, direction=direction, side=side,
                                fair_prob=fair_p, market_price=buy_price,
                                edge=sniper_edge, confidence=0.95,
                                kelly_fraction=kelly_full * 0.7,
                                target_size=size, btc_price=s_now, btc_open=s_open,
                                realized_vol=sigma, d_score=d, vol_zscore=vol_z,
                                filters_passed=4, time_remaining=t_remaining,
                                reasoning=(
                                    f"[{sym_upper}] SNIPER-30s {direction} | "
                                    f"{sym_upper} ${s_now:,.0f} open=${s_open:,.0f} | "
                                    f"|d|={abs_d:.1f} P_fair={fair_p:.3f} vs "
                                    f"mkt={buy_price:.3f} edge={sniper_edge:.4f} | "
                                    f"fee={sniper_fee:.5f} tau={t_remaining:.0f}s | "
                                    f"size=${size:.0f}"
                                ),
                            )
                            signals.append(sig)
                            continue  # sniper ha priorita', skip altri mode per questo mercato

                # ════════════════════════════════════════════════════
                #  MODE 2: OFI MOMENTUM
                #  Entry 30-240s. Trigger: Order Flow Imbalance persistente.
                #  Entra come maker -> no taker fee.
                # ════════════════════════════════════════════════════
                if self.entry_window[0] <= t_elapsed <= self.entry_window[1]:
                    ofi_aligned = (
                        (direction == "UP" and obi > self.ofi_min_imbalance - 0.5
                         and buy_pressure > self.ofi_min_pressure) or
                        (direction == "DOWN" and obi < -(self.ofi_min_imbalance - 0.5)
                         and buy_pressure < (1.0 - self.ofi_min_pressure))
                    )
                    ofi_persistent = self._ofi_is_persistent(direction, self.ofi_persistence_window)

                    if ofi_aligned and ofi_persistent and edge_net >= self.ofi_min_edge:
                        maker_edge = edge_raw
                        if buy_price < self.min_entry_price or buy_price > self.max_entry_price:
                            pass  # skip, fuori range
                        else:
                            kelly_full = max(0.0, (fair_p - buy_price) / (1.0 - buy_price))
                            kelly_half = kelly_full * 0.5
                            size = self.bankroll * kelly_half
                            size = max(self.base_size, min(size, self.max_size))

                            sig = LatencySignal(
                                market=m, direction=direction, side=side,
                                fair_prob=fair_p, market_price=buy_price,
                                edge=maker_edge, confidence=min(0.55 + obi * 0.3, 0.90),
                                kelly_fraction=kelly_half,
                                target_size=size, btc_price=s_now, btc_open=s_open,
                                realized_vol=sigma, d_score=d, vol_zscore=vol_z,
                                filters_passed=3, time_remaining=t_remaining,
                                reasoning=(
                                    f"[{sym_upper}] OFI-MOM {direction} | "
                                    f"{sym_upper} ${s_now:,.0f} open=${s_open:,.0f} | "
                                    f"d={d:+.2f} P_fair={fair_p:.3f} vs "
                                    f"mkt={buy_price:.3f} edge={maker_edge:.4f} (maker) | "
                                    f"OBI={obi:+.2f} TFI={buy_pressure:.2f} "
                                    f"persistent={ofi_persistent} | "
                                    f"tau={t_remaining:.0f}s size=${size:.0f}"
                                ),
                            )
                            signals.append(sig)

                # ════════════════════════════════════════════════════
                #  MODE 3: LATENCY (originale v2.0)
                #  Entry 30-240s. Trigger: vol surge + filtri conferma.
                # ════════════════════════════════════════════════════
                if self.entry_window[0] <= t_elapsed <= self.entry_window[1]:
                    if buy_price > self.max_entry_price or buy_price < self.min_entry_price:
                        continue
                    if edge_net < self.min_edge:
                        continue

                    filters_passed = 0
                    if vol_z >= self.min_vol_zscore:
                        filters_passed += 1
                    if (direction == "UP" and accel > 0) or \
                       (direction == "DOWN" and accel < 0):
                        filters_passed += 1
                    if (direction == "UP" and buy_pressure > 0.58) or \
                       (direction == "DOWN" and buy_pressure < 0.42):
                        filters_passed += 1
                    if (direction == "UP" and obi > 0.10) or \
                       (direction == "DOWN" and obi < -0.10):
                        filters_passed += 1

                    if filters_passed < self.min_filters:
                        continue

                    confidence = min(0.5 + filters_passed * 0.1, 0.95)
                    kelly_full = max(0.0, (fair_p - buy_price) / (1.0 - buy_price))
                    kelly_half = kelly_full * 0.5
                    size = self.bankroll * kelly_half
                    size = max(self.base_size, min(size, self.max_size))

                    sig = LatencySignal(
                        market=m, direction=direction, side=side,
                        fair_prob=fair_p, market_price=buy_price,
                        edge=edge_net, confidence=confidence,
                        kelly_fraction=kelly_half, target_size=size,
                        btc_price=s_now, btc_open=s_open, realized_vol=sigma,
                        d_score=d, vol_zscore=vol_z, filters_passed=filters_passed,
                        time_remaining=t_remaining,
                        reasoning=(
                            f"[{sym_upper}] LATENCY {direction} | "
                            f"{sym_upper} ${s_now:,.0f} open=${s_open:,.0f} "
                            f"ret={log_return:+.5f} | "
                            f"d={d:+.2f} P_fair={fair_p:.3f} vs "
                            f"mkt={buy_price:.3f} edge={edge_net:.4f} | "
                            f"sigma={sigma:.6f}/s tau={t_remaining:.0f}s | "
                            f"vol_z={vol_z:.1f} accel={accel:+.6f} "
                            f"TFI={buy_pressure:.2f} OBI={obi:+.2f} | "
                            f"filters={filters_passed}/4 "
                            f"kelly={kelly_half:.4f} size=${size:.0f}"
                        ),
                    )
                    signals.append(sig)

                self._signal_history.append({
                    "time": now, "dir": direction, "d": d,
                    "fair": fair_p, "mkt": buy_price, "edge": edge_net,
                    "vol_z": vol_z, "filters": 0, "sigma": sigma,
                    "obi": obi, "bp": buy_pressure, "symbol": crypto_symbol,
                })

            signals.sort(key=lambda s: s.edge, reverse=True)

            if signals:
                best = signals[0]
                logger.info(f"[{log_prefix}] {best.reasoning}")
            elif crypto_markets and int(now) % 30 < 8:
                s_now = sd.price
                slot_epoch = None
                try:
                    slug = getattr(crypto_markets[0], "slug", "")
                    slot_epoch = int(slug.split("-")[-1])
                except Exception:
                    pass
                s_open = self._slot_opens.get((sym_upper, slot_epoch), s_now) if slot_epoch else s_now
                lr = math.log(s_now / s_open) if s_open > 0 else 0
                logger.info(
                    f"[{log_prefix}] no edge | {sym_upper}=${s_now:,.0f} "
                    f"open=${s_open:,.0f} ret={lr:+.6f} "
                    f"sigma={sigma:.7f} vol_z={vol_z:.1f} "
                    f"OBI={obi:+.2f} TFI={buy_pressure:.2f} "
                    f"mkts={len(crypto_markets)}"
                )

            all_signals.extend(signals)

        all_signals.sort(key=lambda s: s.edge, reverse=True)
        return all_signals

    # ══════════════════════════════════════════════════════════════
    #  Quantitative Helpers
    # ══════════════════════════════════════════════════════════════

    def _realized_vol(self, btc, window: int = 60) -> float:
        """
        Realized volatility per-second (Andersen-Bollerslev 1998).

        RV = sum(log_return_i^2)  over window
        sigma_per_sec = sqrt(RV / total_seconds)

        v11.0: Adaptive window + deduplicated ticks.
        - Deduplicates consecutive identical prices (noise from illiquid periods)
        - Falls back to longer windows (120s, 300s) if 60s has < 5 unique prices
        - Minimum unique prices threshold to avoid sigma=0 on stale data
        """
        if len(btc.history) < 10:
            return 0.0

        now = time.time()

        # Try progressively longer windows until we get enough unique prices
        for w in [window, 120, 300]:
            cutoff = now - w
            raw_prices = [(ts, p) for ts, p in btc.history if ts >= cutoff]

            if len(raw_prices) < 10:
                continue

            # Deduplicate: keep only ticks where price actually changed
            deduped = [raw_prices[0]]
            for i in range(1, len(raw_prices)):
                if raw_prices[i][1] != raw_prices[i - 1][1]:
                    deduped.append(raw_prices[i])

            # Need at least 5 unique price changes for meaningful vol
            if len(deduped) >= 5:
                # Realized variance on deduplicated ticks
                rv = 0.0
                for i in range(1, len(deduped)):
                    if deduped[i - 1][1] > 0:
                        lr = math.log(deduped[i][1] / deduped[i - 1][1])
                        rv += lr * lr

                total_time = deduped[-1][0] - deduped[0][0]
                if total_time > 0:
                    return math.sqrt(rv / total_time)

        return 0.0

    def _get_slot_open(self, slot_epoch: int, btc, symbol: str = "BTC") -> float | None:
        """
        Prezzo della crypto all'apertura del slot. Registra alla prima osservazione.
        v13.1: Uses (symbol, epoch) key to avoid mixing BTC/ETH/SOL/XRP prices.
        """
        key = (symbol.upper(), slot_epoch)
        if key in self._slot_opens:
            return self._slot_opens[key]

        # Cerca in history il prezzo piu' vicino a slot_epoch
        best_price = None
        best_dt = float("inf")
        for ts, price in btc.history:
            dt = abs(ts - slot_epoch)
            if dt < best_dt:
                best_dt = dt
                best_price = price

        if best_price and best_dt < 60:
            self._slot_opens[key] = best_price
            return best_price

        # Fallback: se siamo nei primi 30s del slot, usa prezzo corrente
        now = time.time()
        if now - slot_epoch < 30:
            self._slot_opens[key] = btc.price
            return btc.price

        return None

    def _volume_zscore(self, btc, short_window: int = 5, long_window: int = 30) -> float:
        """
        Volume anomaly detection via Z-score.

        Z = (vol_rate_short - mean_rate_long) / std_rate_long

        vol_rate = $ volume per second.
        Z > 2.0 indica volume surge (probabile informed trading).
        """
        if not btc.trade_flow:
            return 0.0

        now = time.time()

        # Volume rate nel short window (ultimi 5s)
        short_vol = sum(
            qty * price
            for ts, price, qty, _ in btc.trade_flow
            if now - ts <= short_window
        )
        short_rate = short_vol / max(short_window, 1)

        # Volume per-second buckets nel long window
        buckets: dict[int, float] = {}
        for ts, price, qty, _ in btc.trade_flow:
            if now - ts > long_window:
                continue
            bucket = int(ts)
            buckets[bucket] = buckets.get(bucket, 0) + qty * price

        if len(buckets) < 10:
            return 0.0

        vals = list(buckets.values())
        mean_long = sum(vals) / len(vals)
        if mean_long < 1:
            return 0.0

        var_long = sum((v - mean_long) ** 2 for v in vals) / len(vals)
        std_long = math.sqrt(var_long) if var_long > 0 else mean_long * 0.5

        return (short_rate - mean_long) / std_long if std_long > 0 else 0.0

    def _price_acceleration(self, sd, symbol: str = "btc") -> float:
        """
        Accelerazione del prezzo: derivata seconda.

        accel = momentum(2s) - momentum(5s)

        Positivo = prezzo sta accelerando verso l'alto (inizio di un move).
        Negativo = prezzo sta accelerando verso il basso.
        Vicino a zero = move costante o in esaurimento.

        L'accelerazione cattura l'INIZIO di un move, a differenza del
        momentum che cattura il move gia' avvenuto.
        """
        mom_2 = self.binance.momentum(2, symbol)
        mom_5 = self.binance.momentum(5, symbol)
        return mom_2 - mom_5

    def _calc_obi(self, btc) -> float:
        """
        Order Book Imbalance dai top-5 livelli del depth stream.

        OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol)

        Range [-1, +1]. Positivo = pressione bid (bullish).
        """
        if not btc.depth.bids or not btc.depth.asks:
            return 0.0
        bid_vol = sum(qty for _, qty in btc.depth.bids)
        ask_vol = sum(qty for _, qty in btc.depth.asks)
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def _calc_buy_pressure(self, btc, window: int = 10) -> float:
        """
        Net buy pressure dagli ultimi N secondi di trade flow.

        buy_pressure = buy_aggressor_volume / total_volume

        > 0.58 = forte pressione acquisto (conferma UP)
        < 0.42 = forte pressione vendita (conferma DOWN)
        ~ 0.50 = neutro

        Usa window corto (10s) perche' per latency arb servono
        le informazioni piu' recenti, non la media storica.
        """
        now = time.time()
        cutoff = now - window
        buy_vol = sell_vol = 0.0
        for ts, price, qty, is_buyer_maker in btc.trade_flow:
            if ts < cutoff:
                continue
            if is_buyer_maker:
                sell_vol += qty * price  # seller aggressor
            else:
                buy_vol += qty * price   # buyer aggressor
        total = buy_vol + sell_vol
        return buy_vol / total if total > 0 else 0.5

    # ══════════════════════════════════════════════════════════════
    #  Execution
    # ══════════════════════════════════════════════════════════════

    async def execute(self, signal: LatencySignal, paper: bool = True) -> bool:
        """Esegui un trade di latency arbitrage."""
        now = time.time()
        if now - self._recently_traded.get(signal.market.id, 0) < self.cooldown_sec:
            return False

        token_key = signal.side.lower()
        token_id = signal.market.tokens.get(token_key)
        if not token_id:
            logger.warning(f"[BTC-LATENCY] Token ID non trovato per {token_key}")
            return False

        buy_price = signal.market_price
        size = signal.target_size

        allowed, reason = self.risk.can_trade(
            STRATEGY_NAME, size, price=buy_price,
            side=f"BUY_{signal.side}", market_id=signal.market.id,
        )
        if not allowed:
            logger.info(f"[BTC-LATENCY] Risk block: {reason}")
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
            # Il fair_prob del modello BS e' la nostra stima della probabilita'
            # reale. Per simulare realisticamente, applichiamo un model decay
            # (il modello non e' perfetto: 70% informativo, 30% noise).
            model_decay = 0.70
            sim_prob = signal.fair_prob * model_decay + 0.5 * (1 - model_decay)
            won = random.random() < sim_prob

            # Slippage: taker sweep = ~1.5% slippage medio
            slippage = 0.985
            if won:
                pnl = size * ((1.0 / buy_price) - 1.0) * slippage
            else:
                pnl = -size * slippage

            # Fee crypto
            fee = size * buy_price * 0.25 * (buy_price * (1 - buy_price)) ** 2
            pnl -= fee

            logger.info(
                f"[PAPER] BTC-LATENCY: {signal.direction} "
                f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                f"P_fair={signal.fair_prob:.3f} d={signal.d_score:+.2f} "
                f"edge={signal.edge:.4f} kelly={signal.kelly_fraction:.4f} | "
                f"{'WIN' if won else 'LOSS'} pnl=${pnl:+.2f} (fee=${fee:.2f}) | "
                f"BTC=${signal.btc_price:,.0f} sigma={signal.realized_vol:.6f}"
            )

            self.risk.open_trade(trade)
            self.risk.close_trade(token_id, won=won, pnl=pnl)

            self._pnl_tracker[signal.market.id] = {
                "time": now,
                "direction": signal.direction,
                "side": signal.side,
                "price": buy_price,
                "size": size,
                "fair_prob": signal.fair_prob,
                "d_score": signal.d_score,
                "edge": signal.edge,
                "kelly": signal.kelly_fraction,
                "confidence": signal.confidence,
                "vol_zscore": signal.vol_zscore,
                "filters": signal.filters_passed,
                "sigma": signal.realized_vol,
                "won": won,
                "pnl": pnl,
                "btc_price": signal.btc_price,
                "btc_open": signal.btc_open,
            }
        else:
            # v14.0: FastExecutor -> Horizon -> native smart_buy
            target = min(buy_price + 0.03, self.max_entry_price)
            shares = round(size / target, 2) if target > 0 else 1.0
            if shares < 1:
                shares = 1.0

            executed = False

            # Try 1: FastExecutor (pre-signed, ~80ms)
            if self.fast_executor is not None:
                try:
                    fe_result = self.fast_executor.execute(
                        token_id=token_id,
                        side="BUY",
                        price=target,
                        size=shares,
                        strategy="btc_latency",
                    )
                    if fe_result.success:
                        if fe_result.fill_price > 0:
                            trade.price = fe_result.fill_price
                        self.risk.open_trade(trade)
                        logger.info(
                            f"[LIVE] BTC-LATENCY [FAST-{fe_result.method.upper()}]: {signal.direction} "
                            f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                            f"P_fair={signal.fair_prob:.3f} edge={signal.edge:.4f} | "
                            f"BTC=${signal.btc_price:,.0f} | latency={fe_result.latency_ms:.0f}ms"
                        )
                        executed = True
                except Exception as e:
                    logger.warning(f"[BTC-LATENCY] FastExecutor error, trying fallback: {e}")

            # Try 2: Horizon SDK (if FastExecutor failed or unavailable)
            if not executed and self.horizon is not None:
                hz_result = self.horizon.execute_trade(
                    token_id=token_id,
                    side=f"BUY_{signal.side}",
                    size=size,
                    price=target,
                    strategy="btc_latency",
                    allow_dead_book=True,
                )
                if hz_result.success:
                    if hz_result.fill_price > 0:
                        trade.price = hz_result.fill_price
                    self.risk.open_trade(trade)
                    logger.info(
                        f"[LIVE] BTC-LATENCY [{hz_result.engine.upper()}]: {signal.direction} "
                        f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                        f"P_fair={signal.fair_prob:.3f} edge={signal.edge:.4f} | "
                        f"BTC=${signal.btc_price:,.0f}"
                    )
                    executed = True
                else:
                    logger.warning(f"[BTC-LATENCY] Horizon failed: {hz_result.error}")

            # Try 3: Legacy native smart_buy
            if not executed:
                result = self.api.smart_buy(
                    token_id, size,
                    target_price=target,
                )
                if result:
                    if isinstance(result, dict) and result.get("_fill_price"):
                        trade.price = result["_fill_price"]
                    self.risk.open_trade(trade)
                    logger.info(
                        f"[LIVE] BTC-LATENCY [NATIVE]: {signal.direction} "
                        f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                        f"P_fair={signal.fair_prob:.3f} edge={signal.edge:.4f} | "
                        f"BTC=${signal.btc_price:,.0f}"
                    )
                    executed = True
                else:
                    logger.warning(f"[BTC-LATENCY] Ordine fallito: {signal.market.id}")
                    return False

        self._recently_traded[signal.market.id] = now
        self._trades_executed += 1
        return True

    # ══════════════════════════════════════════════════════════════
    #  Stats & Analytics
    # ══════════════════════════════════════════════════════════════

    @property
    def stats(self) -> dict:
        """Statistiche per dashboard e analisi."""
        if not self._pnl_tracker:
            return {"trades": self._trades_executed, "pnl": 0, "wr": 0}

        trades = list(self._pnl_tracker.values())
        wins = sum(1 for t in trades if t.get("won"))
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        avg_edge = sum(t.get("edge", 0) for t in trades) / len(trades)
        avg_d = sum(abs(t.get("d_score", 0)) for t in trades) / len(trades)

        return {
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "wr": wins / len(trades) * 100 if trades else 0,
            "pnl": total_pnl,
            "avg_pnl": total_pnl / len(trades) if trades else 0,
            "avg_edge": avg_edge,
            "avg_abs_d": avg_d,
            "avg_size": sum(t.get("size", 0) for t in trades) / len(trades) if trades else 0,
            "avg_sigma": sum(t.get("sigma", 0) for t in trades) / len(trades) if trades else 0,
        }
