"""
Strategia: BTC Latency Arbitrage — v2.0 (Quant Rewrite)
========================================================

Modello di segnale — Black-Scholes per binary outcome:

  P(Up) = Phi(d),  dove  d = ln(S_t / S_0) / (sigma * sqrt(tau))

  - S_t   = prezzo BTC corrente (Binance real-time)
  - S_0   = prezzo BTC all'apertura del 5-min slot
  - sigma = realized volatility per-second (Andersen-Bollerslev 1998)
  - tau   = secondi rimanenti nel slot
  - Phi   = CDF della normale standard

  Edge = P_fair - P_market  (gap tra valore reale e prezzo stale Polymarket)

Filtri di conferma (il segnale BS da solo non basta — serve conferma
che il movimento e' reale e informato, non noise):

  1. Volume Z-score   — surge detection: move reale vs rumore
  2. Price acceleration — derivata seconda: move in corso vs esaurito
  3. Trade flow imbalance — aggressor side allineato alla direzione
  4. OBI (Order Book Imbalance) — book conferma pressione direzionale

Sizing — Half-Kelly per binary market (Kelly 1956):

  f* = 0.5 * (p - c) / (1 - c)

  dove p = fair probability, c = costo (prezzo Polymarket).
  Half-Kelly riduce varianza del 75% sacrificando solo 25% del rendimento atteso.

Timing — entry solo tra 30s e 240s nel slot:
  - Primi 30s: open price incerto, modello impreciso
  - 30-240s: finestra ottimale, prezzo Polymarket stale vs informazione Binance
  - Ultimi 60s: mercato efficiente, no edge residuo

Riferimenti:
  Black, Scholes (1973) — probability of finishing ITM
  Andersen, Bollerslev (1998) — realized volatility from tick data
  Kyle (1985) — price impact and informed trading
  Kelly (1956) — optimal fraction sizing
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

    # ── Parametri ──
    bankroll: float = 5000.0         # capitale dedicato a questa strategia
    base_size: float = 100.0         # $ minimo per trade
    max_size: float = 2000.0         # $ massimo per trade
    min_edge: float = 0.03           # 3% edge minimo netto fee
    min_vol_zscore: float = 1.0      # Z-score volume minimo (surge detection)
    min_filters: int = 2             # minimo filtri confermanti su 4
    max_entry_price: float = 0.80    # non comprare sopra
    min_entry_price: float = 0.52    # non comprare sotto (troppo vicino a 50/50)
    entry_window: tuple = (30, 240)  # secondi nel slot: entry tra 30s e 240s
    cooldown_sec: float = 60.0       # un solo trade per slot
    trade_hours: tuple = (0, 0, 23, 59)  # 24/7

    # ── State ──
    _trades_executed: int = 0
    _recently_traded: dict = field(default_factory=dict)
    _pnl_tracker: dict = field(default_factory=dict)
    _signal_history: deque = field(default_factory=lambda: deque(maxlen=500))
    _market_cache: dict = field(default_factory=dict)   # slug -> (Market, expire_ts)
    _slot_opens: dict = field(default_factory=dict)      # epoch -> btc_price

    # ── Constants ──
    GAMMA_API = "https://gamma-api.polymarket.com/markets"
    SLOT_DURATION = 300  # 5 minuti

    # ══════════════════════════════════════════════════════════════
    #  Market Discovery (unchanged from v1.0 — slug pattern works)
    # ══════════════════════════════════════════════════════════════

    def discover_btc_markets(self) -> list[Market]:
        """
        Scopre mercati BTC Up/Down 5-min attivi usando il pattern slug.
        Slug: btc-updown-5m-{epoch} dove epoch e' allineato a 300s.
        Fetcha il slot corrente e i prossimi 2 (15 min di copertura).
        """
        now = int(time.time())
        current_slot = now - (now % self.SLOT_DURATION)
        markets = []

        for offset in range(3):
            epoch = current_slot + offset * self.SLOT_DURATION
            slug = f"btc-updown-5m-{epoch}"

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
                    f"[BTC-DISCOVERY] Found: {question} | "
                    f"prices={prices} slug={slug}"
                )
            except Exception as e:
                logger.debug(f"[BTC-DISCOVERY] Error fetching {slug}: {e}")
                continue

        if markets:
            logger.info(f"[BTC-DISCOVERY] {len(markets)} mercati attivi trovati")

        return markets

    # ══════════════════════════════════════════════════════════════
    #  Core: Scan con modello quantitativo
    # ══════════════════════════════════════════════════════════════

    def scan(self, shared_markets: list[Market] | None = None) -> list[LatencySignal]:
        """
        Scan quantitativo:
        1. Calcola realized vol da tick data (Andersen-Bollerslev)
        2. Per ogni slot attivo, calcola fair P(Up) via Black-Scholes
        3. Confronta con prezzo Polymarket → edge
        4. Filtra con volume surge, acceleration, TFI, OBI
        5. Size via Half-Kelly
        """
        if self.binance.symbol_price("btc") == 0:
            return []

        btc_markets = self.discover_btc_markets()
        if not btc_markets:
            return []

        btc = self.binance.get_symbol("btc")
        now = time.time()

        # ── 1. Realized volatility (per-second) ──
        sigma = self._realized_vol(btc, window=60)
        if sigma < 1e-8:
            if int(now) % 60 < 8:
                # Count unique prices in last 300s for diagnostics
                cutoff = now - 300
                recent = [p for ts, p in btc.history if ts >= cutoff]
                unique = len(set(recent)) if recent else 0
                logger.info(
                    f"[BTC-LATENCY] sigma too low: {sigma:.10f} "
                    f"(ticks={len(btc.history)} unique_5m={unique})"
                )
            return []  # non abbastanza dati tick

        # ── 2. Microstructure features (calcolati una volta per ciclo) ──
        vol_z = self._volume_zscore(btc, short_window=5, long_window=30)
        accel = self._price_acceleration(btc)
        buy_pressure = self._calc_buy_pressure(btc, window=10)
        obi = self._calc_obi(btc)

        # Cleanup slot opens vecchi (>10 min)
        stale = [e for e in self._slot_opens if now - e > 600]
        for e in stale:
            del self._slot_opens[e]

        signals = []

        for m in btc_markets:
            # Cooldown per mercato
            if now - self._recently_traded.get(m.id, 0) < self.cooldown_sec:
                continue

            # Parse slot epoch dal slug
            slug = getattr(m, "slug", "")
            try:
                slot_epoch = int(slug.split("-")[-1])
            except (ValueError, IndexError):
                continue

            # ── Timing gate ──
            t_elapsed = now - slot_epoch
            t_remaining = (slot_epoch + self.SLOT_DURATION) - now

            if t_elapsed < self.entry_window[0] or t_elapsed > self.entry_window[1]:
                continue
            if t_remaining <= 0:
                continue

            # ── Open price del slot ──
            s_open = self._get_slot_open(slot_epoch, btc)
            if s_open is None or s_open <= 0:
                continue

            s_now = btc.price

            # ═══════════════════════════════════════════════
            #  BLACK-SCHOLES: P(Up) = Phi(d)
            #  d = ln(S_t / S_0) / (sigma * sqrt(tau))
            # ═══════════════════════════════════════════════
            log_return = math.log(s_now / s_open)
            vol_term = sigma * math.sqrt(t_remaining)

            if vol_term < 1e-10:
                fair_up = 1.0 if log_return > 0 else 0.0
                d = 999.0 if log_return > 0 else -999.0
            else:
                d = log_return / vol_term
                fair_up = _norm_cdf(d)

            # ── Edge: quale side ha valore? ──
            price_yes = m.prices.get("yes", 0.5)   # prezzo "Up"
            price_no = m.prices.get("no", 0.5)      # prezzo "Down"

            edge_up = fair_up - price_yes            # edge se compriamo Up
            edge_down = (1.0 - fair_up) - price_no   # edge se compriamo Down

            if edge_up >= edge_down:
                direction = "UP"
                side = "YES"
                edge_raw = edge_up
                buy_price = price_yes
                fair_p = fair_up
            else:
                direction = "DOWN"
                side = "NO"
                edge_raw = edge_down
                buy_price = price_no
                fair_p = 1.0 - fair_up

            # Price bounds
            if buy_price > self.max_entry_price or buy_price < self.min_entry_price:
                continue

            # Fee crypto: p * feeRate * (p*(1-p))^exponent
            fee = buy_price * 0.25 * (buy_price * (1 - buy_price)) ** 2
            edge_net = edge_raw - fee

            if edge_net < self.min_edge:
                continue

            # ── Filtri di conferma ──
            filters_passed = 0

            # 1. Volume surge (il move e' accompagnato da volume anomalo?)
            if vol_z >= self.min_vol_zscore:
                filters_passed += 1

            # 2. Price acceleration (il move sta continuando, non esaurendosi?)
            if (direction == "UP" and accel > 0) or \
               (direction == "DOWN" and accel < 0):
                filters_passed += 1

            # 3. Trade flow (gli aggressor trades confermano la direzione?)
            if (direction == "UP" and buy_pressure > 0.58) or \
               (direction == "DOWN" and buy_pressure < 0.42):
                filters_passed += 1

            # 4. OBI (l'order book conferma la pressione direzionale?)
            if (direction == "UP" and obi > 0.10) or \
               (direction == "DOWN" and obi < -0.10):
                filters_passed += 1

            if filters_passed < self.min_filters:
                continue

            # Confidence: base da edge magnitude + filter bonus
            confidence = min(0.5 + filters_passed * 0.1, 0.95)

            # ═══════════════════════════════════════════════
            #  HALF-KELLY SIZING
            #  f* = (p - c) / (1 - c)  per binary market
            #  Half-Kelly = f* / 2
            # ═══════════════════════════════════════════════
            kelly_full = max(0.0, (fair_p - buy_price) / (1.0 - buy_price))
            kelly_half = kelly_full * 0.5

            size = self.bankroll * kelly_half
            size = max(self.base_size, min(size, self.max_size))

            sig = LatencySignal(
                market=m,
                direction=direction,
                side=side,
                fair_prob=fair_p,
                market_price=buy_price,
                edge=edge_net,
                confidence=confidence,
                kelly_fraction=kelly_half,
                target_size=size,
                btc_price=s_now,
                btc_open=s_open,
                realized_vol=sigma,
                d_score=d,
                vol_zscore=vol_z,
                filters_passed=filters_passed,
                time_remaining=t_remaining,
                reasoning=(
                    f"BTC ${s_now:,.0f} (open=${s_open:,.0f} "
                    f"ret={log_return:+.5f}) | "
                    f"BS: d={d:+.2f} P_fair={fair_p:.3f} vs "
                    f"mkt={buy_price:.3f} -> edge={edge_net:.4f} | "
                    f"sigma={sigma:.6f}/s tau={t_remaining:.0f}s | "
                    f"vol_z={vol_z:.1f} accel={accel:+.6f} "
                    f"TFI={buy_pressure:.2f} OBI={obi:+.2f} | "
                    f"filters={filters_passed}/4 "
                    f"kelly={kelly_half:.4f} size=${size:.0f}"
                ),
            )
            signals.append(sig)
            self._signal_history.append({
                "time": now,
                "dir": direction,
                "d": d,
                "fair": fair_p,
                "mkt": buy_price,
                "edge": edge_net,
                "vol_z": vol_z,
                "filters": filters_passed,
                "sigma": sigma,
            })

        # Sort by edge (best opportunity first)
        signals.sort(key=lambda s: s.edge, reverse=True)

        if signals:
            best = signals[0]
            logger.info(f"[BTC-LATENCY] {best.direction} | {best.reasoning}")
        elif btc_markets and int(now) % 30 < 8:
            # Log diagnostico ogni ~60s quando non c'e' edge
            s_now = btc.price
            slot_epoch = None
            try:
                slug = getattr(btc_markets[0], "slug", "")
                slot_epoch = int(slug.split("-")[-1])
            except Exception:
                pass
            s_open = self._slot_opens.get(slot_epoch, s_now) if slot_epoch else s_now
            lr = math.log(s_now / s_open) if s_open > 0 else 0
            logger.info(
                f"[BTC-LATENCY] no edge | BTC=${s_now:,.0f} "
                f"open=${s_open:,.0f} ret={lr:+.6f} "
                f"sigma={sigma:.7f} vol_z={vol_z:.1f} "
                f"mkts={len(btc_markets)}"
            )

        return signals

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

    def _get_slot_open(self, slot_epoch: int, btc) -> float | None:
        """
        Prezzo BTC all'apertura del slot. Registra alla prima osservazione.
        Cerca nel history di Binance il prezzo piu' vicino al timestamp di
        apertura. Fallback: prezzo corrente se siamo nei primi 30s.
        """
        if slot_epoch in self._slot_opens:
            return self._slot_opens[slot_epoch]

        # Cerca in history il prezzo piu' vicino a slot_epoch
        best_price = None
        best_dt = float("inf")
        for ts, price in btc.history:
            dt = abs(ts - slot_epoch)
            if dt < best_dt:
                best_dt = dt
                best_price = price

        if best_price and best_dt < 60:
            self._slot_opens[slot_epoch] = best_price
            return best_price

        # Fallback: se siamo nei primi 30s del slot, usa prezzo corrente
        now = time.time()
        if now - slot_epoch < 30:
            self._slot_opens[slot_epoch] = btc.price
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

    def _price_acceleration(self, btc) -> float:
        """
        Accelerazione del prezzo: derivata seconda.

        accel = momentum(2s) - momentum(5s)

        Positivo = prezzo sta accelerando verso l'alto (inizio di un move).
        Negativo = prezzo sta accelerando verso il basso.
        Vicino a zero = move costante o in esaurimento.

        L'accelerazione cattura l'INIZIO di un move, a differenza del
        momentum che cattura il move gia' avvenuto.
        """
        mom_2 = self.binance.momentum(2, "btc")
        mom_5 = self.binance.momentum(5, "btc")
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
            # Live: sweep orderbook con smart_buy
            result = self.api.smart_buy(
                token_id, size,
                target_price=min(buy_price + 0.03, self.max_entry_price),
            )
            if result:
                if isinstance(result, dict) and result.get("_fill_price"):
                    trade.price = result["_fill_price"]
                self.risk.open_trade(trade)
                logger.info(
                    f"[LIVE] BTC-LATENCY: {signal.direction} "
                    f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                    f"P_fair={signal.fair_prob:.3f} edge={signal.edge:.4f} | "
                    f"BTC=${signal.btc_price:,.0f}"
                )
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
