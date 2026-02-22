"""
Strategia 2: Data-Driven Prediction + LunarCrush + CryptoQuant + Nansen
=========================================================================
Usa feed esterni (Binance, LunarCrush, CryptoQuant, Nansen) per stimare la
probabilita' "vera" di un outcome e confrontarla col prezzo Polymarket.

Ispirata al trader "ilovecircle": $2.2M di profitto, 74% win rate.

Per i mercati crypto:
- Prezzo Binance = fonte di verita' real-time
- Galaxy Score LunarCrush = sentiment sociale (bullish/bearish bias)
- MVRV CryptoQuant = mercato sopra/sottovalutato (modifica probabilita')
- Exchange Flows CryptoQuant = whale accumulazione/distribuzione
- Smart Money Nansen = cosa fanno i trader piu' profittevoli (probability + confidence)

Per i mercati non-crypto: usa il volume e le variazioni di prezzo
come segnali di informazione asimmetrica (insider movement).
"""

import logging
import random
import time
from dataclasses import dataclass

from utils.polymarket_api import Market, PolymarketAPI
from utils.binance_feed import BinanceFeed
from utils.lunarcrush_feed import LunarCrushFeed
from utils.cryptoquant_feed import CryptoQuantFeed
from utils.nansen_feed import NansenFeed
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "data_driven"


@dataclass
class Prediction:
    """Una predizione su un mercato."""
    market: Market
    true_prob_yes: float  # Probabilita' stimata vera
    market_prob_yes: float  # Probabilita' implicita dal prezzo
    edge_yes: float
    edge_no: float
    best_side: str  # "YES" o "NO"
    best_edge: float
    confidence: float
    reasoning: str


class DataDrivenStrategy:
    """
    Identifica mercati dove la probabilita' "vera" diverge
    significativamente dal prezzo Polymarket.

    Per mercati crypto: usa il feed Binance come fonte di verita'.
    Per altri mercati: analizza variazioni anomale di volume/prezzo
    come indicatori di informazione asimmetrica.
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        binance: BinanceFeed,
        lunar: LunarCrushFeed | None = None,
        cquant: CryptoQuantFeed | None = None,
        nansen: NansenFeed | None = None,
        min_edge: float = 0.05,  # v5.9.1: mercati non-crypto sono FEE-FREE → edge 5%
    ):
        self.api = api
        self.risk = risk
        self.binance = binance
        self.lunar = lunar
        self.cquant = cquant
        self.nansen = nansen
        self.min_edge = min_edge
        self._predictions: list[Prediction] = []
        self._trades_executed = 0
        self._recently_traded: dict[str, float] = {}  # market_id -> timestamp
        self._TRADE_COOLDOWN = 86400  # v5.9.4: 24h — MAI ri-tradare stesso mercato nello stesso giorno

    async def analyze(self, shared_markets: list[Market] | None = None) -> list[Prediction]:
        """
        Analizza i mercati e genera predizioni.
        Ritorna solo quelle con edge > soglia minima.
        Accetta mercati pre-fetchati per evitare chiamate API duplicate.
        """
        predictions = []
        n_crypto = 0
        n_general = 0

        # Usa mercati condivisi se disponibili
        all_markets = shared_markets or self.api.fetch_markets(limit=200)

        if not all_markets:
            logger.info("[DATA] Analyze: 0 mercati disponibili — nessuna analisi")
            return []

        # v4.1.2: Filtra mercati crypto — ora supporta BTC, ETH, SOL, XRP
        # Ogni mercato viene abbinato al simbolo corretto per il feed Binance.
        crypto_kw = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple"]
        crypto_markets = [
            m for m in all_markets
            if any(kw in m.question.lower() for kw in crypto_kw)
        ]
        crypto_ids = {m.id for m in crypto_markets}

        for m in crypto_markets:
            # v5.4: skip mercati morti (price < $0.01 = illiquido/dead)
            price_yes = m.prices.get("yes", 0.5)
            price_no = m.prices.get("no", 0.5)
            if price_yes < 0.01 or price_no < 0.01:
                continue
            pred = self._analyze_crypto_market(m)
            if pred and pred.best_edge > self.min_edge:
                predictions.append(pred)
                n_crypto += 1

        # Mercati generali (escludi quelli crypto gia' analizzati)
        for m in all_markets:
            if m.id in crypto_ids:
                continue
            # v5.4: skip mercati morti
            price_yes = m.prices.get("yes", 0.5)
            price_no = m.prices.get("no", 0.5)
            if price_yes < 0.01 or price_no < 0.01:
                continue
            pred = self._analyze_general_market(m)
            if pred and pred.best_edge > self.min_edge:
                predictions.append(pred)
                n_general += 1

        predictions.sort(key=lambda p: p.best_edge, reverse=True)
        self._predictions = predictions

        if predictions:
            logger.info(
                f"[DATA] Analyze {len(all_markets)} mercati → "
                f"{len(predictions)} predizioni (crypto: {n_crypto}, general: {n_general}) "
                f"(migliore: {predictions[0].best_edge:.4f} su "
                f"'{predictions[0].market.question[:40]}')"
            )
        else:
            logger.info(
                f"[DATA] Analyze {len(all_markets)} mercati → 0 predizioni "
                f"(crypto scansionati: {len(crypto_markets)}, "
                f"generali scansionati: {len(all_markets) - len(crypto_markets)}, "
                f"binance={'OK' if self.binance.price > 0 else 'NO FEED'})"
            )

        return predictions

    def _analyze_crypto_market(self, market: Market) -> Prediction | None:
        """
        Analizza un mercato crypto usando il feed Binance.
        v4.1.2: supporta BTC, ETH, SOL, XRP (non solo BTC).
        """
        question = market.question.lower()

        # Identifica il simbolo crypto del mercato
        symbol = self._detect_crypto_symbol(question)
        sym_data = self.binance.get_symbol(symbol)
        if sym_data.price == 0:
            return None

        market_prob = market.prices.get("yes", 0.5)

        # Estrai la soglia di prezzo dalla domanda
        threshold = self._extract_threshold(question)
        if threshold is None:
            return None

        # Calcola quanto il prezzo e' vicino alla soglia
        crypto_price = sym_data.price
        distance_pct = (crypto_price - threshold) / threshold

        # Momentum e direzione (metodi su BinanceFeed, non SymbolData)
        direction, confidence = self.binance.direction_confidence(symbol=symbol)
        momentum = self.binance.momentum(30, symbol=symbol)
        vol = self.binance.volatility(60, symbol=symbol)

        # v7.0: NON tradare se direction=FLAT e momentum~0.
        # Il bot stava generando trade senza segnale direzionale,
        # producendo edge artificiale dalla sola distanza prezzo/soglia.
        if direction == "FLAT" and abs(momentum) < 0.0001:
            return None

        # Stima probabilita' vera
        # Se BTC e' gia' sopra la soglia e il momentum e' positivo,
        # la probabilita' che resti sopra e' alta
        if "above" in question or "over" in question or "sopra" in question:
            true_prob = self._estimate_above_probability(
                distance_pct, momentum, vol, confidence, direction
            )
        elif "below" in question or "under" in question or "sotto" in question:
            true_prob = 1.0 - self._estimate_above_probability(
                distance_pct, momentum, vol, confidence, direction
            )
        else:
            true_prob = 0.5 + distance_pct * 0.3  # Fallback generico
            true_prob = max(0.05, min(0.95, true_prob))

        # ── LunarCrush Galaxy Score Adjustment ──
        # Il Galaxy Score (0-100) combina price action + sentiment + social activity.
        # Lo usiamo per aggiustare la probabilita' stimata.
        gs_tag = ""
        if self.lunar:
            cs = self.lunar.get_sentiment("btc")
            if cs.is_fresh and cs.galaxy_score > 0:
                gs = cs.galaxy_score
                gs_tag = f" | GS={gs:.0f} Sent={cs.sentiment:.0f}%"

                # GS > 65: bias bullish — aumenta prob "above"
                if gs > 65:
                    gs_boost = min((gs - 65) / 100 * 0.04, 0.04)
                    if "above" in question or "over" in question:
                        true_prob = min(true_prob + gs_boost, 0.98)
                    else:
                        true_prob = max(true_prob - gs_boost, 0.02)

                # GS < 35: bias bearish — aumenta prob "below"
                elif gs < 35:
                    gs_penalty = min((35 - gs) / 100 * 0.04, 0.04)
                    if "above" in question or "over" in question:
                        true_prob = max(true_prob - gs_penalty, 0.02)
                    else:
                        true_prob = min(true_prob + gs_penalty, 0.98)

                # Sentiment forte → confidence boost
                if cs.social_momentum in ("STRONG_BULL", "STRONG_BEAR"):
                    confidence = min(confidence * 1.10, 0.85)

        # ── CryptoQuant MVRV + Exchange Flow Adjustment ──
        # MVRV: se il mercato e' sopra/sottovalutato, modifica la probabilita'.
        # Exchange flows: whale accumulazione/distribuzione.
        oc_tag = ""
        if self.cquant:
            oc = self.cquant.get_onchain("btc")
            if oc.is_fresh:
                oc_tag = f" | MVRV={oc.mvrv:.2f}({oc.mvrv_signal}) Flow={oc.flow_signal}"

                # MVRV adjustment — segnale macro
                # MVRV > 2.5 = sopravvalutato → penalizza prob "above"
                # MVRV < 1.0 = sottovalutato → boost prob "above"
                mvrv_adj = 0.0
                if oc.mvrv > 2.5:
                    mvrv_adj = -min((oc.mvrv - 2.5) / 5.0 * 0.05, 0.05)
                elif oc.mvrv < 1.0 and oc.mvrv > 0:
                    mvrv_adj = min((1.0 - oc.mvrv) / 0.5 * 0.05, 0.05)

                if mvrv_adj != 0:
                    if "above" in question or "over" in question:
                        true_prob = max(0.02, min(0.98, true_prob + mvrv_adj))
                    else:
                        true_prob = max(0.02, min(0.98, true_prob - mvrv_adj))

                # Exchange flow: conferma/smentisce direzione
                flow_dir = oc.flow_direction
                if abs(flow_dir) > 0.3:
                    # Outflow (flow_dir < 0) = bullish → leggero boost "above"
                    flow_adj = -flow_dir * 0.02  # max ±0.02
                    if "above" in question or "over" in question:
                        true_prob = max(0.02, min(0.98, true_prob + flow_adj))
                    else:
                        true_prob = max(0.02, min(0.98, true_prob - flow_adj))

        # ── Nansen Smart Money Adjustment ──
        # Lo smart money (fondi, trader profittevoli) precede il prezzo.
        # Se i trader migliori stanno comprando → bullish bias.
        # Se stanno vendendo → bearish bias.
        # Multi-segment agreement = segnale piu' forte.
        sm_tag = ""
        if self.nansen:
            sm = self.nansen.get_smart_money("btc")
            if sm.is_fresh and sm.smart_money_signal != "UNKNOWN":
                sm_dir = sm.smart_money_direction  # -1.0 a +1.0
                sm_tag = f" | SM={sm.smart_money_signal} NF24h=${sm.net_flow_24h/1e6:+.1f}M"

                # Smart money direction adjusts probability
                if abs(sm_dir) > 0.3:
                    # Smart money buying (sm_dir > 0) → boost prob "above"
                    # Smart money selling (sm_dir < 0) → reduce prob "above"
                    sm_adj = sm_dir * 0.03  # max ±0.03
                    if "above" in question or "over" in question:
                        true_prob = max(0.02, min(0.98, true_prob + sm_adj))
                    else:
                        true_prob = max(0.02, min(0.98, true_prob - sm_adj))

                # Multi-segment agreement: se whale + smart trader + exchange concordano
                agreement = sm.multi_segment_agreement
                if agreement > 0.7:
                    # Alto accordo tra segmenti → confidence boost
                    confidence = min(confidence * 1.08, 0.90)

                # Trend consistency: 24h e 7d nella stessa direzione
                if sm.trend_consistency > 0.5:
                    confidence = min(confidence * 1.05, 0.90)

        edge_yes = true_prob - market_prob
        edge_no = (1 - true_prob) - market.prices.get("no", 0.5)

        if max(edge_yes, edge_no) < self.min_edge:
            return None

        best_side = "YES" if edge_yes > edge_no else "NO"
        best_edge = max(edge_yes, edge_no)

        # v7.0: Edge cap ridotto a 6% + penalita' confidence se edge alto.
        # Se TUTTI i trade hanno edge=cap, il modello e' sistematicamente overconfident.
        # Il mercato ha quasi sempre piu' informazione di noi.
        MAX_CRYPTO_EDGE = 0.06
        if best_edge > MAX_CRYPTO_EDGE:
            # Penalizza confidence proporzionalmente — edge alto = overconfidence
            overconf_ratio = best_edge / MAX_CRYPTO_EDGE
            confidence *= max(0.5, 1.0 / overconf_ratio)
            best_edge = MAX_CRYPTO_EDGE
            edge_yes = min(edge_yes, MAX_CRYPTO_EDGE)
            edge_no = min(edge_no, MAX_CRYPTO_EDGE)

        return Prediction(
            market=market,
            true_prob_yes=true_prob,
            market_prob_yes=market_prob,
            edge_yes=edge_yes,
            edge_no=edge_no,
            best_side=best_side,
            best_edge=best_edge,
            confidence=confidence,
            reasoning=(
                f"{symbol.upper()}=${crypto_price:,.0f} vs soglia=${threshold:,.0f} "
                f"(dist={distance_pct:+.3f}) | "
                f"Dir={direction} Mom={momentum:+.5f} Vol={vol:.6f}{gs_tag}{oc_tag}"
            ),
        )

    def _estimate_above_probability(
        self,
        distance_pct: float,
        momentum: float,
        volatility: float,
        confidence: float,
        direction: str,
    ) -> float:
        """
        Stima la probabilita' che il prezzo sia sopra la soglia.
        Combina distanza corrente, momentum, e volatilita'.

        IMPORTANTE: k adattivo in base alla distanza.
        - Mercati dove BTC e' vicino alla soglia (<2%): k alto, edge sfruttabile
        - Mercati dove BTC e' lontano (>5%): k basso, il mercato ha ragione
        """
        import math

        # k ADATTIVO: se il prezzo e' lontano dalla soglia, non forzare
        # verso probabilita' intermedie. Il mercato ha ragione sui long-term.
        abs_dist = abs(distance_pct)
        if abs_dist < 0.02:
            k = 80  # Molto vicino: piccole variazioni contano molto
        elif abs_dist < 0.05:
            k = 40  # Vicino: buon edge potenziale
        elif abs_dist < 0.10:
            k = 20  # Moderato: edge limitato
        else:
            k = 8   # Lontano: il mercato ha ragione, non forzare

        base = 1.0 / (1.0 + math.exp(-k * distance_pct))

        # Aggiustamento per momentum — RIDOTTO rispetto a prima
        # Max 5% di aggiustamento (era 15%)
        momentum_adj = 0.0
        if direction == "UP":
            momentum_adj = min(momentum * 5 * confidence, 0.02)  # v5.9: ridotto da 20/0.05 a 5/0.02
        elif direction == "DOWN":
            momentum_adj = max(momentum * 5 * confidence, -0.02)  # v5.9: ridotto

        # Aggiustamento per volatilita' — RIDOTTO drasticamente
        # Alta vol NON deve spingere verso 0.5 per mercati lontani dalla soglia
        vol_adj = 0.0
        if volatility > 0.0005 and abs_dist < 0.05:
            # Solo per mercati VICINI alla soglia: vol = incertezza
            vol_adj = (0.5 - base) * min(volatility * 30, 0.10)

        prob = base + momentum_adj + vol_adj
        return max(0.02, min(0.98, prob))

    def _analyze_general_market(self, market: Market) -> Prediction | None:
        """
        Analizza mercati non-crypto cercando anomalie di prezzo.
        Segnali di informazione asimmetrica:
        - Mispricing: YES + NO != 1.0
        - Spread anomalo: ampio = incertezza = opportunita'
        - Mercati sbilanciati con volume significativo
        """
        market_prob = market.prices.get("yes", 0.5)
        price_no = market.prices.get("no", 0.5)
        total = market_prob + price_no

        # Segnale 1: Mispricing YES + NO != 1.0
        # Soglia abbassata da 0.03 a 0.015, volume minimo da 10000 a 1000
        if abs(total - 1.0) > 0.015 and market.volume > 1000:
            true_prob = market_prob / total if total > 0 else 0.5
            edge_yes = true_prob - market_prob
            edge_no = (1 - true_prob) - price_no
            best_side = "YES" if edge_yes > edge_no else "NO"
            best_edge = max(edge_yes, edge_no)

            if best_edge > self.min_edge:
                confidence = 0.55 if market.volume < 5000 else 0.65
                return Prediction(
                    market=market,
                    true_prob_yes=true_prob,
                    market_prob_yes=market_prob,
                    edge_yes=edge_yes,
                    edge_no=edge_no,
                    best_side=best_side,
                    best_edge=best_edge,
                    confidence=confidence,
                    reasoning=f"Mispricing: YES+NO={total:.4f} vol=${market.volume:,.0f}",
                )

        # Segnale 2: Spread anomalo con prezzo sbilanciato
        # Mercati dove il prezzo e' molto sbilanciato (< 0.15 o > 0.85)
        # e la deviazione da 1.0 e' significativa
        if market.volume > 2000 and market.spread > 0.02:
            if market_prob > 0.85 or market_prob < 0.15:
                # Probabile overconfidence — piccolo edge contrarian
                if market_prob > 0.85:
                    edge = (market_prob - 0.80) * 0.2
                    if edge > self.min_edge:
                        return Prediction(
                            market=market,
                            true_prob_yes=market_prob - edge,
                            market_prob_yes=market_prob,
                            edge_yes=-edge,
                            edge_no=edge,
                            best_side="NO",
                            best_edge=edge,
                            confidence=0.50,
                            reasoning=f"Spread+overconfidence: YES@{market_prob:.4f} spread={market.spread:.4f}",
                        )
                else:
                    edge = (0.20 - market_prob) * 0.2
                    if edge > self.min_edge:
                        return Prediction(
                            market=market,
                            true_prob_yes=market_prob + edge,
                            market_prob_yes=market_prob,
                            edge_yes=edge,
                            edge_no=-edge,
                            best_side="YES",
                            best_edge=edge,
                            confidence=0.50,
                            reasoning=f"Spread+overconfidence: YES@{market_prob:.4f} spread={market.spread:.4f}",
                        )

        return None

    @staticmethod
    def _detect_crypto_symbol(question: str) -> str:
        """Identifica il simbolo crypto dalla domanda. Default: btc."""
        q = question.lower()
        if "ethereum" in q or "eth" in q:
            return "eth"
        if "solana" in q or "sol" in q:
            return "sol"
        if "xrp" in q or "ripple" in q:
            return "xrp"
        return "btc"

    def _extract_threshold(self, question: str) -> float | None:
        """Estrai una soglia numerica dalla domanda del mercato."""
        import re
        patterns = [
            r"\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)",
            r"(?:above|over|below|under|sopra|sotto)\s*\$?([\d,]+(?:\.\d+)?)",
            r"\$\s*([\d,]+(?:\.\d+)?)",
        ]
        for p in patterns:
            match = re.search(p, question)
            if match:
                try:
                    val = float(match.group(1).replace(",", ""))
                    if "k" in question[match.start():match.end()].lower():
                        val *= 1000
                    return val
                except ValueError:
                    continue
        return None

    async def execute(self, prediction: Prediction, paper: bool = True) -> bool:
        """Esegui un trade basato su una predizione."""
        # DEDUPLICAZIONE: non tradare lo stesso mercato troppo frequentemente
        now = time.time()
        market_id = prediction.market.id
        last_traded = self._recently_traded.get(market_id, 0)
        if now - last_traded < self._TRADE_COOLDOWN:
            remaining = int(self._TRADE_COOLDOWN - (now - last_traded))
            logger.info(
                f"[DATA] Cooldown: '{prediction.market.question[:40]}' "
                f"— ancora {remaining}s"
            )
            return False

        # v5.4: Non ri-comprare mercati dove abbiamo gia' una posizione
        for open_t in self.risk.open_trades:
            if open_t.market_id == market_id:
                logger.info(
                    f"[DATA] Skip: posizione gia' aperta su "
                    f"'{prediction.market.question[:40]}'"
                )
                return False

        token_key = "yes" if prediction.best_side == "YES" else "no"
        token_id = prediction.market.tokens[token_key]
        price = prediction.market.prices[token_key]

        size = self.risk.kelly_size(
            win_prob=prediction.true_prob_yes if prediction.best_side == "YES" else (1 - prediction.true_prob_yes),
            price=price,
            strategy=STRATEGY_NAME,
            is_maker=True,  # v5.3: smart_buy usa maker-first
        )

        if size == 0:
            logger.info(
                f"[DATA] kelly_size=0 per '{prediction.market.question[:40]}' "
                f"price={price:.4f} true_prob={prediction.true_prob_yes:.4f} "
                f"edge={prediction.best_edge:.4f} side={prediction.best_side} "
                f"budget=${self.risk._strategy_budgets.get(STRATEGY_NAME, 0):.0f}"
            )
            return False

        allowed, reason = self.risk.can_trade(
            STRATEGY_NAME, size, price=price,
            side=f"BUY_{prediction.best_side}", market_id=prediction.market.id,
        )
        if not allowed:
            logger.info(f"Trade bloccato: {reason}")
            return False

        trade = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id=prediction.market.id,
            token_id=token_id,
            side=f"BUY_{prediction.best_side}",
            size=size,
            price=price,
            edge=prediction.best_edge,
            reason=prediction.reasoning,
        )

        if paper:
            logger.info(
                f"[PAPER] DATA-DRIVEN: BUY {prediction.best_side} "
                f"'{prediction.market.question[:40]}' "
                f"${size:.2f} @{price:.4f} edge={prediction.best_edge:.4f}"
            )
            self.risk.open_trade(trade)

            # Simulazione BINARIA realistica (v4.1 — formula unificata):
            # Payoff binario come Polymarket reale:
            #   WIN  → guadagno = size * (1/price - 1) * slippage
            #   LOSS → perdita  = -size * slippage
            # Win prob basata su true_prob stimata dalla strategia
            import random
            true_p = prediction.true_prob_yes if prediction.best_side == "YES" \
                else (1 - prediction.true_prob_yes)
            sim_win_prob = min(max(true_p, 0.30), 0.75)
            won = random.random() < sim_win_prob
            slippage = 0.92 + random.random() * 0.06  # 92-98%
            if won:
                pnl = size * ((1.0 / price) - 1.0) * slippage
            else:
                pnl = -size * slippage
            self.risk.close_trade(token_id, won=won, pnl=pnl)
        else:
            result = self.api.smart_buy(
                token_id, size, target_price=price,
                timeout_sec=12.0, fallback_market=True,
            )
            if result:
                # v7.4: Aggiorna prezzo con fill reale dal CLOB
                if isinstance(result, dict) and result.get("_fill_price"):
                    trade.price = result["_fill_price"]
                self.risk.open_trade(trade)

        # Registra timestamp per deduplicazione
        self._recently_traded[market_id] = now
        self._trades_executed += 1
        return True
