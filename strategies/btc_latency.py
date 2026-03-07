"""
Strategia: BTC Latency Arbitrage — v1.0 (Prototipo)
====================================================
Ispirata a Female-Bongo ($508K profit, 11K trade, 59% WR).

Meccanismo:
1. Monitora BTC price su Binance WS in real-time (sub-100ms)
2. Rileva movimento direzionale forte (momentum + OBI + trade flow)
3. Compra il favorito (Up o Down) su mercati Polymarket 5-min
4. Il mercato Polymarket reagisce con 3-10s di ritardo
5. In quel gap c'e' alpha

Differenze dal vecchio crypto_5min:
- Nessun filtro sentiment/LunarCrush/CryptoQuant (troppo lento)
- Signal puramente da price action Binance (sub-second)
- Sizing aggressivo ($500-$5000 per slot, non $25)
- Sweep orderbook (taker), non limit maker
- Solo BTC (piu' liquido, meno noise)

Fee: crypto mercati hanno fee ~0.8% round-trip a p=0.50.
Con entry a p=0.70, fee one-way ~0.4%. Serve edge > 0.5%.

Capitale richiesto: minimo $5K dedicati.
"""

import logging
import math
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field

import requests

from utils.polymarket_api import Market, PolymarketAPI
from utils.binance_feed import BinanceFeed
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "btc_latency"

# Pattern per mercati Bitcoin 5-min "Up or Down"
BTC_5MIN_PATTERNS = [
    re.compile(r"bitcoin\s+up\s+or\s+down", re.I),
    re.compile(r"btc\s+up\s+or\s+down", re.I),
]

# Timeframe pattern: "2:20PM-2:25PM" = 5 min
TIME_SLOT_PATTERN = re.compile(
    r"(\d{1,2}):(\d{2})(AM|PM)\s*-\s*(\d{1,2}):(\d{2})(AM|PM)\s*(ET|EST|EDT)",
    re.I,
)


@dataclass
class LatencySignal:
    """Segnale di latency arbitrage."""
    market: Market
    direction: str          # "UP" o "DOWN"
    side: str               # "YES" per Up-market, "NO" per Down-market
    confidence: float       # 0-1
    edge: float             # stimato
    btc_price: float        # prezzo BTC al momento del segnale
    btc_momentum_5s: float  # momentum 5s
    btc_momentum_15s: float # momentum 15s
    obi: float              # order book imbalance
    buy_pressure: float     # net buy pressure (0-1)
    target_size: float      # $ da investire
    reasoning: str


@dataclass
class BTCLatencyStrategy:
    """
    Latency arbitrage su mercati Bitcoin Up/Down 5-min.

    Signal generation:
    - Momentum 5s/15s/30s da Binance trade stream
    - OBI (Order Book Imbalance) da depth stream
    - Net buy/sell pressure da trade flow
    - Tutti devono concordare sulla direzione

    Entry:
    - Compra il favorito (Up o Down) quando segnale forte
    - Sweep orderbook (market buy fino a target size)
    - Max prezzo entry: $0.82 (sopra = rischio troppo alto)

    Exit:
    - Hold fino a settlement (5 min slot)
    - No exit anticipato per ora (v1.0)
    """

    api: PolymarketAPI
    risk: RiskManager
    binance: BinanceFeed

    # ── Parametri ──
    base_size: float = 500.0          # $ per trade (scaling graduale)
    max_size: float = 5000.0          # $ max per slot
    max_entry_price: float = 0.82     # non comprare sopra (payoff troppo basso)
    min_entry_price: float = 0.55     # non comprare sotto (segnale debole)
    min_momentum_strength: float = 0.0003  # min |momentum_5s| per triggerare
    min_obi_strength: float = 0.15    # min |OBI| per conferma
    min_confidence: float = 0.55      # soglia confidence
    cooldown_sec: float = 60.0        # cooldown per mercato (un 5-min slot)
    trade_hours: tuple = (13, 0, 23, 0)  # 8AM-6PM ET = 13:00-23:00 UTC

    # ── State ──
    _trades_executed: int = 0
    _recently_traded: dict = field(default_factory=dict)
    _pnl_tracker: dict = field(default_factory=dict)  # tracking per analisi
    _signal_history: deque = field(default_factory=lambda: deque(maxlen=500))
    _market_cache: dict = field(default_factory=dict)  # slug -> (Market, expire_ts)

    # ── Market Discovery ──
    GAMMA_API = "https://gamma-api.polymarket.com/markets"
    SLOT_DURATION = 300  # 5 minuti

    def discover_btc_markets(self) -> list[Market]:
        """
        Scopre mercati BTC Up/Down 5-min attivi usando il pattern slug.
        Slug: btc-updown-5m-{epoch} dove epoch e' allineato a 300s.
        Fetcha il slot corrente e i prossimi 2 (15 min di copertura).
        """
        import datetime
        now = int(time.time())
        current_slot = now - (now % self.SLOT_DURATION)
        markets = []

        # Slot corrente + prossimi 2
        for offset in range(3):
            epoch = current_slot + offset * self.SLOT_DURATION
            slug = f"btc-updown-5m-{epoch}"

            # Cache check (evita fetch ripetuti)
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

                # Gamma API ritorna lista
                item = data[0] if isinstance(data, list) else data

                # Costruisci Market object
                condition_id = item.get("conditionId", item.get("condition_id", ""))
                question = item.get("question", "")
                outcomes = item.get("outcomes", ["Up", "Down"])
                if isinstance(outcomes, str):
                    import json as _json
                    try:
                        outcomes = _json.loads(outcomes)
                    except Exception:
                        outcomes = ["Up", "Down"]

                # Token IDs
                clob_token_ids = item.get("clobTokenIds", item.get("clob_token_ids", []))
                if isinstance(clob_token_ids, str):
                    import json as _json
                    try:
                        clob_token_ids = _json.loads(clob_token_ids)
                    except Exception:
                        clob_token_ids = []

                # Prices
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
                    id=condition_id,
                    question=question,
                    outcomes=outcomes,
                    tokens=tokens,
                    prices=prices,
                    volume=float(item.get("volume", 0)),
                    slug=slug,
                )

                # Cache per 60s (i prezzi cambiano)
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

    def scan(self, shared_markets: list[Market] | None = None) -> list[LatencySignal]:
        """Scansiona per opportunita' di latency arbitrage."""
        # Check: Binance feed BTC pronto
        if self.binance.symbol_price("btc") == 0:
            return []

        # Check: siamo in orario di trading?
        import datetime
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        hour_utc = now_utc.hour + now_utc.minute / 60.0
        start_h = self.trade_hours[0] + self.trade_hours[1] / 60.0
        end_h = self.trade_hours[2] + self.trade_hours[3] / 60.0
        if not (start_h <= hour_utc <= end_h):
            return []

        # Scopri mercati BTC 5-min direttamente (non dipende da shared_markets)
        btc_markets = self.discover_btc_markets()
        if not btc_markets:
            return []

        # Genera segnale da Binance price action
        signal_dir, signal_conf, signal_data = self._generate_signal()
        if signal_dir is None or signal_conf < self.min_confidence:
            return []

        now = time.time()
        signals = []

        for m in btc_markets:
            # Cooldown
            if now - self._recently_traded.get(m.id, 0) < self.cooldown_sec:
                continue

            # Determina side: se mercato e' "Up", e il segnale e' UP → compra YES
            side = self._determine_side(m, signal_dir)
            if side is None:
                continue

            # Prezzo
            buy_price = m.prices.get(side.lower(), 0.5)
            if buy_price > self.max_entry_price or buy_price < self.min_entry_price:
                continue

            # Edge stimato: momentum-based
            # Female-Bongo fa 59% WR comprando a 0.70 avg
            # Edge = WR_true - price = 0.59 - price
            # Con segnale forte (conf > 0.7): WR stima ~62%
            # Con segnale medio (conf 0.55-0.7): WR stima ~56%
            estimated_wr = 0.50 + signal_conf * 0.15  # max ~0.635
            edge = estimated_wr - buy_price

            # Fee crypto: C * p * feeRate * (p*(1-p))^exponent
            # feeRate=0.25, exponent=2
            fee_one_way = buy_price * 0.25 * (buy_price * (1 - buy_price)) ** 2
            edge_net = edge - fee_one_way

            if edge_net < 0.01:  # almeno 1% edge netto
                continue

            # Sizing: scala con confidence
            size = self.base_size * (signal_conf / 0.55)  # boost per alta conf
            size = min(size, self.max_size)

            # Payoff check: non comprare se payoff ratio < 0.20
            payoff = (1.0 / buy_price) - 1.0 if buy_price > 0 else 0
            if payoff < 0.20:
                continue

            sig = LatencySignal(
                market=m,
                direction=signal_dir,
                side=side,
                confidence=signal_conf,
                edge=edge_net,
                btc_price=signal_data["btc_price"],
                btc_momentum_5s=signal_data["mom_5s"],
                btc_momentum_15s=signal_data["mom_15s"],
                obi=signal_data["obi"],
                buy_pressure=signal_data["buy_pressure"],
                target_size=size,
                reasoning=(
                    f"BTC ${signal_data['btc_price']:,.0f} | "
                    f"DIR={signal_dir} conf={signal_conf:.3f} | "
                    f"mom5s={signal_data['mom_5s']:+.5f} "
                    f"mom15s={signal_data['mom_15s']:+.5f} | "
                    f"OBI={signal_data['obi']:+.3f} "
                    f"buyP={signal_data['buy_pressure']:.3f} | "
                    f"edge={edge_net:.4f} price={buy_price:.3f} "
                    f"fee={fee_one_way:.4f} payoff={payoff:.2f}x"
                ),
            )
            signals.append(sig)
            self._signal_history.append({
                "time": now,
                "dir": signal_dir,
                "conf": signal_conf,
                "btc": signal_data["btc_price"],
                "edge": edge_net,
            })

        if signals:
            best = signals[0]
            logger.info(
                f"[BTC-LATENCY] {len(btc_markets)} mercati, "
                f"{len(signals)} segnali | "
                f"best: {best.direction} @{best.market.prices.get(best.side.lower(), 0):.3f} "
                f"edge={best.edge:.4f} conf={best.confidence:.3f} "
                f"size=${best.target_size:.0f} | {best.reasoning}"
            )

        return signals

    def _generate_signal(self) -> tuple[str | None, float, dict]:
        """
        Genera segnale direzionale da Binance price action.

        Combina:
        1. Momentum 5s/15s/30s (weighted)
        2. OBI (Order Book Imbalance)
        3. Net buy/sell pressure dagli ultimi 30s di trade flow

        Tutti devono concordare per un segnale forte.
        """
        btc = self.binance.get_symbol("btc")
        if btc.price == 0:
            return None, 0, {}

        # 1. Momentum multi-timeframe
        mom_5s = self.binance.momentum(5, "btc")
        mom_15s = self.binance.momentum(15, "btc")
        mom_30s = self.binance.momentum(30, "btc")

        # Weighted momentum score
        weighted_mom = mom_5s * 0.50 + mom_15s * 0.30 + mom_30s * 0.20

        # Minimo momentum per triggerare
        if abs(weighted_mom) < self.min_momentum_strength:
            return None, 0, {}

        # 2. OBI (Order Book Imbalance)
        obi = self._calc_obi(btc)

        # 3. Net buy pressure (ultimi 30s)
        buy_pressure = self._calc_buy_pressure(btc, window=30)

        # Direction e confidence
        direction = "UP" if weighted_mom > 0 else "DOWN"

        # Confidence: tutti devono concordare
        signals_agree = 0
        if direction == "UP":
            if obi > self.min_obi_strength:
                signals_agree += 1
            if buy_pressure > 0.55:
                signals_agree += 1
            if mom_5s > 0 and mom_15s > 0:
                signals_agree += 1
        else:
            if obi < -self.min_obi_strength:
                signals_agree += 1
            if buy_pressure < 0.45:
                signals_agree += 1
            if mom_5s < 0 and mom_15s < 0:
                signals_agree += 1

        # Base confidence da momentum strength
        vol = self.binance.volatility(60, "btc")
        vol_floor = max(vol, 0.00005)
        snr = min(abs(weighted_mom) / vol_floor, 5.0)
        base_conf = 0.40 + snr * 0.06  # 0.40-0.70

        # Agreement boost
        if signals_agree == 3:
            confidence = min(base_conf * 1.25, 0.90)  # tutti concordano
        elif signals_agree == 2:
            confidence = min(base_conf * 1.10, 0.80)  # 2/3 concordano
        elif signals_agree == 1:
            confidence = base_conf * 0.90  # solo 1/3
        else:
            confidence = base_conf * 0.70  # nessuno conferma

        data = {
            "btc_price": btc.price,
            "mom_5s": mom_5s,
            "mom_15s": mom_15s,
            "mom_30s": mom_30s,
            "weighted_mom": weighted_mom,
            "obi": obi,
            "buy_pressure": buy_pressure,
            "snr": snr,
            "signals_agree": signals_agree,
        }

        return direction, confidence, data

    def _calc_obi(self, btc) -> float:
        """Calcola Order Book Imbalance da depth snapshot."""
        if not btc.depth.bids or not btc.depth.asks:
            return 0.0
        bid_vol = sum(qty for _, qty in btc.depth.bids)
        ask_vol = sum(qty for _, qty in btc.depth.asks)
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def _calc_buy_pressure(self, btc, window: int = 30) -> float:
        """Calcola net buy pressure dagli ultimi N secondi di trade flow."""
        now = time.time()
        cutoff = now - window
        buy_vol = 0.0
        sell_vol = 0.0
        for ts, price, qty, is_buyer_maker in btc.trade_flow:
            if ts < cutoff:
                continue
            if is_buyer_maker:
                sell_vol += qty * price  # seller aggressor
            else:
                buy_vol += qty * price   # buyer aggressor
        total = buy_vol + sell_vol
        if total == 0:
            return 0.5
        return buy_vol / total

    def _determine_side(self, market: Market, direction: str) -> str | None:
        """Determina YES/NO in base alla direzione e al tipo di mercato."""
        q = market.question.lower()
        # "Bitcoin Up or Down" → outcomes sono "Up" e "Down"
        outcomes = [o.lower() for o in market.outcomes] if market.outcomes else []

        if direction == "UP":
            # Compra il token "Up"
            if "up" in outcomes:
                return "YES"  # YES = Up wins
            return "YES"  # default: YES = up
        else:
            # Compra il token "Down"
            if "down" in outcomes:
                # In Polymarket, per mercato Up/Down binario:
                # YES = Up, NO = Down
                return "NO"
            return "NO"

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

        buy_price = signal.market.prices.get(token_key, 0.5)
        size = signal.target_size

        # Risk check
        allowed, reason = self.risk.can_trade(
            STRATEGY_NAME, size, price=buy_price,
            side=f"BUY_{signal.side}", market_id=signal.market.id,
        )
        if not allowed:
            logger.info(f"[BTC-LATENCY] Bloccato: {reason}")
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
            # Paper trading con simulazione realistica
            # Female-Bongo: 59% WR complessivo
            # Con conf > 0.7: ~65% WR
            # Con conf 0.55-0.7: ~55% WR
            sim_wr = 0.50 + signal.confidence * 0.15
            won = random.random() < sim_wr

            # Slippage: sweep orderbook = ~1-3% slippage
            slippage = 0.98  # 2% slippage medio
            if won:
                pnl = size * ((1.0 / buy_price) - 1.0) * slippage
            else:
                pnl = -size * slippage

            # Fee crypto
            fee = size * buy_price * 0.25 * (buy_price * (1 - buy_price)) ** 2
            pnl -= fee

            logger.info(
                f"[PAPER] BTC-LATENCY: {signal.direction} "
                f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} "
                f"edge={signal.edge:.4f} conf={signal.confidence:.3f} "
                f"→ {'WIN' if won else 'LOSS'} pnl=${pnl:+.2f} (fee=${fee:.2f}) | "
                f"BTC=${signal.btc_price:,.0f}"
            )

            self.risk.open_trade(trade)
            self.risk.close_trade(token_id, won=won, pnl=pnl)

            # Track per analisi
            self._pnl_tracker[signal.market.id] = {
                "time": now,
                "direction": signal.direction,
                "side": signal.side,
                "price": buy_price,
                "size": size,
                "edge": signal.edge,
                "confidence": signal.confidence,
                "won": won,
                "pnl": pnl,
                "btc_price": signal.btc_price,
            }
        else:
            # Live trading — sweep orderbook
            # Usa smart_buy con target price = max_entry_price (accetta slippage)
            result = self.api.smart_buy(
                token_id, size, target_price=min(buy_price + 0.03, self.max_entry_price),
            )
            if result:
                if isinstance(result, dict) and result.get("_fill_price"):
                    trade.price = result["_fill_price"]
                self.risk.open_trade(trade)
                logger.info(
                    f"[LIVE] BTC-LATENCY: {signal.direction} "
                    f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} "
                    f"edge={signal.edge:.4f} | BTC=${signal.btc_price:,.0f}"
                )
            else:
                logger.warning(f"[BTC-LATENCY] Ordine fallito per {signal.market.id}")
                return False

        self._recently_traded[signal.market.id] = now
        self._trades_executed += 1
        return True

    @property
    def stats(self) -> dict:
        """Statistiche per dashboard."""
        if not self._pnl_tracker:
            return {"trades": self._trades_executed, "pnl": 0, "wr": 0}

        trades = list(self._pnl_tracker.values())
        wins = sum(1 for t in trades if t.get("won"))
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        return {
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "wr": wins / len(trades) * 100 if trades else 0,
            "pnl": total_pnl,
            "avg_pnl": total_pnl / len(trades) if trades else 0,
            "avg_size": sum(t.get("size", 0) for t in trades) / len(trades) if trades else 0,
        }
