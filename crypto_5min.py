"""
Strategia 4: Crypto Short-Term Directional (Multi-Crypto) + LunarCrush + CryptoQuant
======================================================================================
Specializzata nei mercati crypto a breve termine (1 / 5 / 15 min)
su Polymarket: "Will BTC/ETH/SOL/XRP be up/down in the next 5 minutes?"

Approccio:
- Feed Binance real-time multi-crypto (BTC, ETH, SOL, XRP)
- Feed LunarCrush: sentiment sociale e Galaxy Score per ogni crypto
- Feed CryptoQuant: exchange flows on-chain e MVRV
- La direzione a 5 min dipende dal momentum a brevissimo termine
- Il sentiment sociale modula confidenza e edge
- Exchange flows (whale movements) confermano/smentiscono il momentum
- Edge = differenza tra probabilita' stimata e prezzo di mercato
- Trades solo quando il momentum e' FORTE e coerente

Mapping automatico:
- Ogni mercato Polymarket viene associato al simbolo corretto
- "Will BTC be up..." → feed btcusdt + LunarCrush bitcoin + CryptoQuant BTC
- "Will Ethereum be above..." → feed ethusdt + LunarCrush ethereum + CryptoQuant ETH
- "Will SOL..." → feed solusdt + LunarCrush solana

Vantaggi v3.7 (LunarCrush):
- Sentiment sociale precede il prezzo di 3-10 min (documentato)
- Galaxy Score > 70 con momentum UP → confidence boost
- Sentiment < 30 con momentum DOWN → conferma bearish
- Segnali discordanti (momentum UP + sentiment DOWN) → confidence penalty

Vantaggi v3.8 (CryptoQuant):
- Exchange outflow = accumulazione whale → bullish confirmation
- Exchange inflow = distribuzione whale → bearish confirmation
- Whale movements precedono il prezzo di 10-60 min

Vantaggi v4.0 (Nansen):
- Smart money net flow conferma/smentisce direzione momentum
- Multi-segment agreement (whale + smart trader + exchange) → confidence boost
- Nansen vede i trader piu' profittevoli, non solo i piu' grandi
"""

import logging
import math
import random
import re
import time
from dataclasses import dataclass

from utils.polymarket_api import Market, PolymarketAPI
from utils.binance_feed import BinanceFeed
from utils.lunarcrush_feed import LunarCrushFeed, CryptoSentiment
from utils.cryptoquant_feed import CryptoQuantFeed, OnChainData
from utils.nansen_feed import NansenFeed, SmartMoneyFlow
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "crypto_5min"

# ── Mappatura crypto ──────────────────────────────────────────
# Chiave = simbolo interno, Valore = pattern regex con word boundary
# IMPORTANTE: usiamo \b per evitare falsi positivi ("eth" in "whether")
CRYPTO_PATTERNS: dict[str, list[re.Pattern]] = {
    "btc": [re.compile(r"\bbitcoin\b", re.I), re.compile(r"\bbtc\b", re.I)],
    "eth": [re.compile(r"\bethereum\b", re.I), re.compile(r"\beth\b", re.I)],
    "sol": [re.compile(r"\bsolana\b", re.I), re.compile(r"\bsol\b", re.I)],
    "xrp": [re.compile(r"\bxrp\b", re.I), re.compile(r"\bripple\b", re.I)],
}

# Nomi display per il log
CRYPTO_NAMES: dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "xrp": "XRP",
}

# Pattern per filtro generale "e' un mercato crypto?"
CRYPTO_FILTER_PATTERN = re.compile(
    r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|crypto)\b", re.I
)

# Pattern per identificare mercati crypto tradabili (short-term + daily)
SHORT_TERM_PATTERNS = [
    r"5[\s-]?min",
    r"15[\s-]?min",
    r"next\s+\d+\s+minute",
    r"1[\s-]?min",
    r"up\s+or\s+down",
    r"(?:btc|bitcoin|eth|ethereum|sol|solana|xrp|ripple).*(?:up|down)",
    # v4.1.2: mercati crypto giornalieri (prezzo sopra/sotto soglia)
    r"(?:price|prezzo).*(?:above|below|over|under)",
    r"(?:above|below|over|under)\s+\$[\d,.]+",
    r"(?:btc|bitcoin|eth|ethereum|sol|solana|xrp|ripple).*\$[\d,.]+",
    r"(?:will|can).*(?:reach|hit|break|cross)",
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d+",
]


@dataclass
class ShortTermSignal:
    """Segnale di trading per mercati crypto a breve termine."""
    market: Market
    symbol: str         # "btc", "eth", "sol", "xrp"
    side: str           # "YES" o "NO" (= "UP" o "DOWN" a seconda del mercato)
    edge: float
    confidence: float
    true_prob: float
    market_prob: float
    timeframe_min: int  # 1, 5 o 15
    reasoning: str
    sentiment_signal: float = 0.0   # -1.0 a +1.0 da LunarCrush
    galaxy_score: float = 0.0       # 0-100 da LunarCrush


class Crypto5MinStrategy:
    """
    Trading direzionale su mercati crypto a breve termine.

    Funzionamento:
    1. Filtra SOLO mercati con timeframe 1-15 min
    2. Rileva automaticamente il simbolo (BTC/ETH/SOL/XRP)
    3. Usa momentum Binance del simbolo CORRETTO per stimare la direzione
    4. Confronta con il prezzo di mercato
    5. Trada solo quando il momentum e' forte e coerente

    Il vantaggio e' che Binance reagisce in millisecondi,
    mentre i market maker Polymarket aggiornano in secondi/minuti.
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        binance: BinanceFeed,
        lunar: LunarCrushFeed | None = None,
        cquant: CryptoQuantFeed | None = None,
        nansen: NansenFeed | None = None,
        min_edge: float = 0.03,
        min_confidence: float = 0.45,
    ):
        self.api = api
        self.risk = risk
        self.binance = binance
        self.lunar = lunar
        self.cquant = cquant
        self.nansen = nansen
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self._trades_executed = 0
        self._recently_traded: dict[str, float] = {}
        self._TRADE_COOLDOWN = 120  # 2 min cooldown (i mercati durano 5 min)
        self._compiled_patterns = [
            re.compile(p, re.IGNORECASE) for p in SHORT_TERM_PATTERNS
        ]

    async def scan(
        self, shared_markets: list[Market] | None = None
    ) -> list[ShortTermSignal]:
        """Scansiona mercati per opportunita' crypto a breve termine."""
        signals = []
        markets = shared_markets or self.api.fetch_markets(limit=200)

        if not markets:
            logger.info("[5MIN] Scan: 0 mercati disponibili")
            return []

        ready = self.binance.ready_symbols()
        if not ready:
            logger.info("[5MIN] Scan: nessun feed Binance pronto")
            return []

        # Filtra solo mercati a breve termine
        short_markets = self._filter_short_term(markets)

        now = time.time()
        skipped_cooldown = 0
        skipped_no_feed = 0

        for m in short_markets:
            # Check cooldown
            last = self._recently_traded.get(m.id, 0)
            if now - last < self._TRADE_COOLDOWN:
                skipped_cooldown += 1
                continue

            # Rileva simbolo crypto
            symbol = self._detect_symbol(m.question)
            if not symbol or symbol not in ready:
                skipped_no_feed += 1
                continue

            signal = self._analyze(m, symbol)
            if (
                signal
                and signal.edge > self.min_edge
                and signal.confidence > self.min_confidence
            ):
                signals.append(signal)

        signals.sort(key=lambda s: s.edge * s.confidence, reverse=True)

        if signals:
            # Conta segnali per simbolo
            by_sym = {}
            for s in signals:
                by_sym[s.symbol] = by_sym.get(s.symbol, 0) + 1
            sym_str = " ".join(f"{k.upper()}:{v}" for k, v in by_sym.items())
            logger.info(
                f"[5MIN] Scan {len(markets)} mercati "
                f"({len(short_markets)} short-term, {skipped_cooldown} cooldown, "
                f"{skipped_no_feed} no-feed) → "
                f"{len(signals)} segnali [{sym_str}] "
                f"(migliore: {signals[0].symbol.upper()} "
                f"edge={signals[0].edge:.4f} conf={signals[0].confidence:.2f})"
            )
        else:
            logger.info(
                f"[5MIN] Scan {len(markets)} mercati "
                f"({len(short_markets)} short-term, {skipped_cooldown} cooldown, "
                f"{skipped_no_feed} no-feed) → "
                f"0 segnali ({self.binance.prices_summary()})"
            )

        return signals

    def _detect_symbol(self, question: str) -> str | None:
        """
        Rileva quale criptovaluta riguarda il mercato.

        Ritorna "btc", "eth", "sol", "xrp" oppure None.
        Usa word boundary regex per evitare falsi positivi
        (es: "eth" in "whether" NON matcha).
        """
        for sym, patterns in CRYPTO_PATTERNS.items():
            for pat in patterns:
                if pat.search(question):
                    return sym
        return None

    def _filter_short_term(self, markets: list[Market]) -> list[Market]:
        """Filtra solo mercati crypto con timeframe breve (1-15 min)."""
        results = []

        for m in markets:
            q = m.question
            # Deve essere crypto (con word boundary per evitare falsi positivi)
            if not CRYPTO_FILTER_PATTERN.search(q):
                continue
            # Deve essere short-term
            if any(p.search(q) for p in self._compiled_patterns):
                results.append(m)

        return results

    def _detect_timeframe(self, question: str) -> int:
        """Rileva il timeframe del mercato dalla domanda."""
        q = question.lower()
        if "1 min" in q or "1-min" in q:
            return 1
        if "5 min" in q or "5-min" in q:
            return 5
        if "15 min" in q or "15-min" in q:
            return 15
        # Default
        return 5

    def _analyze(self, market: Market, symbol: str) -> ShortTermSignal | None:
        """
        Analizza un mercato crypto a breve termine.

        Per mercati "up or down":
        - YES = il prezzo sale (o resta uguale)
        - NO = il prezzo scende

        L'edge viene dal momentum Binance che precede il prezzo Polymarket.
        Il feed usato dipende dal simbolo rilevato dal mercato.
        """
        q = market.question.lower()
        price_yes = market.prices.get("yes", 0.5)
        price_no = market.prices.get("no", 0.5)
        timeframe = self._detect_timeframe(q)

        # Prezzo corrente del simbolo
        current_price = self.binance.symbol_price(symbol)
        if current_price == 0:
            return None

        # Analisi momentum multi-timeframe PER IL SIMBOLO CORRETTO
        direction, dir_confidence = self.binance.direction_confidence(symbol)

        # Momentum a diversi orizzonti
        mom_5s = self.binance.momentum(5, symbol)
        mom_15s = self.binance.momentum(15, symbol)
        mom_30s = self.binance.momentum(30, symbol)
        vol = self.binance.volatility(60, symbol)

        # Se la direzione e' FLAT o la confidenza e' bassa, skip
        if direction == "FLAT" or dir_confidence < 0.2:
            return None

        # Calcola probabilita' che il prezzo salga nei prossimi N minuti
        # Basato su momentum pesato e volatilita'
        up_score = (
            mom_5s * 0.50
            + mom_15s * 0.30  # Il momentum recente conta di piu'
            + mom_30s * 0.20
        )

        # Normalizza con la volatilita' per ottenere un segnale
        # IMPORTANTE: floor sulla volatilita' per evitare SNR assurdi
        # Vol tipica a 60s: BTC~0.0001-0.001, SOL~0.0003-0.003, ETH~0.0002-0.002
        vol_floor = max(vol, 0.00005)
        snr = abs(up_score) / vol_floor

        # CAP SNR a valori ragionevoli: max 5.0 per evitare saturazione
        # SNR=1 → segnale debole, SNR=3 → forte, SNR=5 → molto forte
        snr = min(snr, 5.0)

        # Converti SNR in probabilita' con sigmoide CONSERVATIVA
        # Fattore 0.12: la probabilita' varia al massimo ±12% da 0.50
        # Questo e' realistico: anche i migliori segnali momentum danno
        # 55-62% di accuratezza a 5 min (non 75%).
        # v4.0.1: ridotto da 0.5 a 0.12 (era la causa di edge sempre a 25%)
        k_tf = {1: 0.40, 5: 0.30, 15: 0.20}.get(timeframe, 0.30)
        if up_score > 0:
            prob_up = (
                0.5 + 0.12 * (1.0 - math.exp(-k_tf * snr)) * dir_confidence
            )
        else:
            prob_up = (
                0.5 - 0.12 * (1.0 - math.exp(-k_tf * snr)) * dir_confidence
            )

        # Cap realistico: 0.38-0.62 (v5.0: ristretto — piu' conservativo)
        prob_up = max(0.38, min(0.62, prob_up))

        # Nome display del simbolo
        sym_name = CRYPTO_NAMES.get(symbol, symbol.upper())

        # Determina il tipo di mercato e calcola l'edge
        if "up" in q and "down" in q:
            # Mercato "up or down": YES = up, NO = down
            edge_yes = prob_up - price_yes
            edge_no = (1 - prob_up) - price_no
        elif "above" in q or "over" in q or "reach" in q:
            # Mercato "above threshold": usa threshold
            threshold = self._extract_threshold(q)
            if threshold is None:
                return None
            dist = (current_price - threshold) / threshold
            # Per short-term, se sei gia' sopra, alta probabilita' di restarci
            if dist > 0:
                prob_above = 0.5 + min(dist * 10, 0.45) * dir_confidence
            else:
                prob_above = 0.5 - min(abs(dist) * 10, 0.45)
            edge_yes = prob_above - price_yes
            edge_no = (1 - prob_above) - price_no
            prob_up = prob_above
        elif "below" in q or "under" in q or "dip" in q:
            threshold = self._extract_threshold(q)
            if threshold is None:
                return None
            dist = (current_price - threshold) / threshold
            if dist < 0:
                prob_below = 0.5 + min(abs(dist) * 10, 0.45) * dir_confidence
            else:
                prob_below = 0.5 - min(dist * 10, 0.45)
            edge_yes = prob_below - price_yes
            edge_no = (1 - prob_below) - price_no
            prob_up = prob_below  # In questo caso prob_up = prob del dip
        else:
            return None

        best_side = "YES" if edge_yes > edge_no else "NO"
        best_edge = max(edge_yes, edge_no)

        # v5.9.4: Fee-awareness — mercati crypto short-term hanno fee dinamiche
        # fee = p * (1-p) * 0.0625 per lato. Sottraiamo la fee stimata dall'edge.
        # Se l'edge netto e' negativo, il trade non e' profittevole dopo fee.
        best_price = price_yes if best_side == "YES" else price_no
        estimated_fee = best_price * (1.0 - best_price) * 0.0625
        best_edge_net = best_edge - estimated_fee
        if best_edge_net < self.min_edge:
            return None
        best_edge = best_edge_net

        # Confidence graduale: scale con SNR e direction confidence
        # SNR=1 → conf~0.40, SNR=3 → conf~0.55, SNR=5 → conf~0.65
        confidence = min(dir_confidence * (0.35 + snr * 0.06), 0.70)

        # ── LunarCrush Sentiment Adjustment ──
        # Il sentiment sociale precede il prezzo di 3-10 min.
        # Usiamolo per modulare confidenza e probabilita'.
        sent_signal = 0.0
        gs_value = 0.0
        social_tag = ""

        if self.lunar:
            cs = self.lunar.get_sentiment(symbol)
            if cs.is_fresh:
                sent_signal = cs.sentiment_signal  # -1.0 a +1.0
                gs_value = cs.galaxy_score
                social_tag = f" | Social={cs.social_momentum} GS={gs_value:.0f} Sent={cs.sentiment:.0f}%"

                # 1. Conferma: momentum e sentiment nella stessa direzione
                #    → boost confidenza fino a +15%
                if direction == "UP" and sent_signal > 0.15:
                    confidence *= 1.0 + min(sent_signal * 0.15, 0.15)
                elif direction == "DOWN" and sent_signal < -0.15:
                    confidence *= 1.0 + min(abs(sent_signal) * 0.15, 0.15)

                # 2. Divergenza: momentum e sentiment in direzioni opposte
                #    → penalty confidenza fino a -20%
                elif direction == "UP" and sent_signal < -0.20:
                    confidence *= max(1.0 - abs(sent_signal) * 0.20, 0.80)
                elif direction == "DOWN" and sent_signal > 0.20:
                    confidence *= max(1.0 - sent_signal * 0.20, 0.80)

                # 3. Galaxy Score estremo: modula probabilita'
                #    GS > 70 → leggero bias bullish (+1-3%)
                #    GS < 30 → leggero bias bearish (-1-3%)
                if gs_value > 70:
                    gs_adj = min((gs_value - 70) / 100 * 0.02, 0.02)
                    prob_up = min(prob_up + gs_adj, 0.62)
                elif gs_value < 30 and gs_value > 0:
                    gs_adj = min((30 - gs_value) / 100 * 0.02, 0.02)
                    prob_up = max(prob_up - gs_adj, 0.38)

                # Ricalcola edge dopo aggiustamento (con fee deduction)
                if "up" in q and "down" in q:
                    edge_yes = prob_up - price_yes
                    edge_no = (1 - prob_up) - price_no
                    best_side = "YES" if edge_yes > edge_no else "NO"
                    best_edge = max(edge_yes, edge_no)
                    # v5.9.4: fee-aware ricalcolo
                    best_price = price_yes if best_side == "YES" else price_no
                    estimated_fee = best_price * (1.0 - best_price) * 0.0625
                    best_edge = best_edge - estimated_fee
                    if best_edge < self.min_edge:
                        return None

        # ── CryptoQuant On-Chain Adjustment ──
        # Exchange flows (whale movements) confermano/smentiscono il momentum.
        # Outflow = accumulazione → bullish. Inflow = distribuzione → bearish.
        onchain_tag = ""

        if self.cquant:
            # BTC on-chain per tutti (il BTC traina l'intero mercato crypto)
            oc = self.cquant.get_onchain("btc")
            if oc.is_fresh:
                flow_dir = oc.flow_direction  # -1.0 (bullish/outflow) a +1.0 (bearish/inflow)
                onchain_tag = f" | Flow={oc.flow_signal} MVRV={oc.mvrv:.2f}"

                # Conferma: whale direction concorda con momentum
                if direction == "UP" and flow_dir < -0.3:
                    # Whale stanno accumulando + momentum UP → boost
                    confidence *= 1.0 + min(abs(flow_dir) * 0.10, 0.10)
                elif direction == "DOWN" and flow_dir > 0.3:
                    # Whale stanno distribuendo + momentum DOWN → boost
                    confidence *= 1.0 + min(flow_dir * 0.10, 0.10)

                # Divergenza: whale direction opposta al momentum
                elif direction == "UP" and flow_dir > 0.5:
                    # Momentum UP ma whale stanno vendendo → penalty
                    confidence *= max(1.0 - flow_dir * 0.12, 0.85)
                elif direction == "DOWN" and flow_dir < -0.5:
                    # Momentum DOWN ma whale stanno comprando → penalty
                    confidence *= max(1.0 - abs(flow_dir) * 0.12, 0.85)

        # ── Nansen Smart Money Confirmation ──
        # Smart money (fondi, top PnL traders) che muovono nella stessa direzione
        # del momentum = conferma forte. Direzione opposta = penalty.
        # Multi-segment agreement (whale + smart trader + exchange) → extra boost.
        nansen_tag = ""

        if self.nansen:
            sm = self.nansen.get_smart_money(symbol)
            if sm.is_fresh and sm.smart_money_signal != "UNKNOWN":
                sm_dir = sm.smart_money_direction  # -1.0 a +1.0
                nansen_tag = f" | SM={sm.smart_money_signal} NF=${sm.net_flow_24h/1e6:+.1f}M"

                # Conferma: smart money concorda con momentum
                if direction == "UP" and sm_dir > 0.3:
                    # Smart money sta comprando + momentum UP → boost
                    confidence *= 1.0 + min(sm_dir * 0.08, 0.08)
                elif direction == "DOWN" and sm_dir < -0.3:
                    # Smart money sta vendendo + momentum DOWN → boost
                    confidence *= 1.0 + min(abs(sm_dir) * 0.08, 0.08)

                # Divergenza: smart money opposta al momentum
                elif direction == "UP" and sm_dir < -0.5:
                    # Momentum UP ma smart money vende → penalty
                    confidence *= max(1.0 - abs(sm_dir) * 0.10, 0.88)
                elif direction == "DOWN" and sm_dir > 0.5:
                    # Momentum DOWN ma smart money compra → penalty
                    confidence *= max(1.0 - sm_dir * 0.10, 0.88)

                # Multi-segment agreement: tutti i segmenti concordano
                if sm.multi_segment_agreement > 0.75:
                    confidence *= min(1.06, 1.0 + sm.multi_segment_agreement * 0.06)

        # Cap finale confidenza
        confidence = min(confidence, 0.85)

        return ShortTermSignal(
            market=market,
            symbol=symbol,
            side=best_side,
            edge=best_edge,
            confidence=confidence,
            true_prob=prob_up,
            market_prob=price_yes,
            timeframe_min=timeframe,
            sentiment_signal=sent_signal,
            galaxy_score=gs_value,
            reasoning=(
                f"{sym_name}=${current_price:,.2f} | "
                f"Dir={direction} conf={dir_confidence:.2f} | "
                f"Mom5={mom_5s:+.6f} Mom15={mom_15s:+.6f} | "
                f"SNR={snr:.2f} Vol={vol:.6f} | "
                f"P_up={prob_up:.4f} vs Mkt={price_yes:.4f} | "
                f"TF={timeframe}min{social_tag}{onchain_tag}{nansen_tag}"
            ),
        )

    def _extract_threshold(self, question: str) -> float | None:
        """Estrai soglia numerica dalla domanda."""
        patterns = [
            r"\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)",
            r"(?:above|over|below|under|reach|dip)\s*(?:to\s*)?\$?([\d,]+(?:\.\d+)?)",
            r"\$\s*([\d,]+(?:\.\d+)?)",
        ]
        for p in patterns:
            match = re.search(p, question)
            if match:
                try:
                    val = float(match.group(1).replace(",", ""))
                    if "k" in question[match.start() : match.end()].lower():
                        val *= 1000
                    return val
                except (ValueError, IndexError):
                    continue
        return None

    async def execute(
        self, signal: ShortTermSignal, paper: bool = True
    ) -> bool:
        """Esegui un trade su un mercato crypto a breve termine."""
        # Deduplicazione
        now = time.time()
        last = self._recently_traded.get(signal.market.id, 0)
        if now - last < self._TRADE_COOLDOWN:
            return False

        # ── FIX v5.0: Anti-contraddizione ──
        # Blocca trade se abbiamo gia' una posizione OPPOSTA sullo stesso mercato
        # (evita di comprare YES e NO sullo stesso mercato = perdita garantita)
        for open_t in self.risk.open_trades:
            if open_t.market_id == signal.market.id:
                existing_side = "YES" if "YES" in open_t.side.upper() else "NO"
                if existing_side != signal.side:
                    logger.info(
                        f"[5MIN] BLOCCATO: anti-contraddizione su {signal.market.id[:16]} "
                        f"(aperto={existing_side}, nuovo={signal.side})"
                    )
                    return False
                # Gia' una posizione nella stessa direzione: skip duplicato
                logger.debug(
                    f"[5MIN] Skip duplicato su {signal.market.id[:16]} ({signal.side})"
                )
                return False

        # ── FIX v5.0: Cap edge al 20% ──
        # Edge > 20% e' quasi certamente un artefatto del modello
        if signal.edge > 0.20:
            logger.debug(
                f"[5MIN] Edge cappato: {signal.edge:.4f} → 0.20 su {signal.market.id[:16]}"
            )
            signal.edge = 0.20

        token_key = "yes" if signal.side == "YES" else "no"
        token_id = signal.market.tokens[token_key]
        price = signal.market.prices[token_key]

        # Kelly sizing — usa la probabilita' stimata
        win_prob = (
            signal.true_prob if signal.side == "YES" else (1 - signal.true_prob)
        )
        size = self.risk.kelly_size(
            win_prob=win_prob,
            price=price,
            strategy=STRATEGY_NAME,
        )

        if size == 0:
            return False

        allowed, reason = self.risk.can_trade(STRATEGY_NAME, size)
        if not allowed:
            logger.info(f"[5MIN] Trade bloccato: {reason}")
            return False

        sym_name = CRYPTO_NAMES.get(signal.symbol, signal.symbol.upper())

        trade = Trade(
            timestamp=now,
            strategy=STRATEGY_NAME,
            market_id=signal.market.id,
            token_id=token_id,
            side=f"BUY_{signal.side}",
            size=size,
            price=price,
            edge=signal.edge,
            reason=signal.reasoning,
        )

        if paper:
            sent_tag = ""
            if signal.galaxy_score > 0:
                sent_tag = f" GS={signal.galaxy_score:.0f} Sent={signal.sentiment_signal:+.2f}"
            logger.info(
                f"[PAPER] 5MIN: {sym_name} BUY {signal.side} "
                f"'{signal.market.question[:50]}' "
                f"${size:.2f} @{price:.4f} edge={signal.edge:.4f} "
                f"conf={signal.confidence:.2f} tf={signal.timeframe_min}min{sent_tag}"
            )
            self.risk.open_trade(trade)

            # Simulazione BINARIA realistica (v4.1 — formula unificata):
            # Payoff binario come Polymarket reale:
            #   WIN  → guadagno = size * (1/price - 1) * slippage
            #   LOSS → perdita  = -size * slippage
            # Win prob = true_prob stimata dalla strategia (non dall'edge)
            sim_win_prob = min(max(signal.true_prob if signal.side == "YES"
                                   else (1 - signal.true_prob), 0.30), 0.75)
            won = random.random() < sim_win_prob

            slippage = 0.92 + random.random() * 0.06  # 92-98% del payoff teorico
            if won:
                pnl = size * ((1.0 / price) - 1.0) * slippage
            else:
                pnl = -size * slippage

            self.risk.close_trade(token_id, won=won, pnl=pnl)
        else:
            result = self.api.buy_market(token_id, size)
            if result:
                self.risk.open_trade(trade)

        self._recently_traded[signal.market.id] = now
        self._trades_executed += 1
        return True

    @property
    def stats(self) -> dict:
        return {
            "trades_executed": self._trades_executed,
            "markets_in_cooldown": sum(
                1
                for t in self._recently_traded.values()
                if time.time() - t < self._TRADE_COOLDOWN
            ),
        }
