"""
Strategia Gabagool — Arbitraggio Puro su Polymarket v5.0
=========================================================
Ispirata ai bot profittevoli documentati ($40M di profitti 2024-2025).
Non predice mai la direzione — trova mispricing matematici.

Due livelli:
1. BASE: YES_ask + NO_ask < 1.00 (dopo fee) → compra entrambi → profitto garantito
2. FRANK-WOLFE: Arbitraggio combinatorio tra mercati correlati (soglie BTC/ETH/SOL)
   Trova combinazioni di posizioni su mercati con soglie diverse che garantiscono
   profitto indipendentemente dal prezzo finale.

Fonti:
- RohOnChain: Adaptive Fully-Corrective Frank-Wolfe + Bregman Projection
- gabagool22 wallet analysis ($2M+ profitti da 4049 trade)
- Paper: "Unravelling the Probabilistic Forest" (86M transazioni analizzate)
"""

import logging
import re
import time
from dataclasses import dataclass, field
from itertools import combinations

from utils.polymarket_api import Market, PolymarketAPI
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "arb_gabagool"

# ── Fee Polymarket (aggiornato Feb 2026) ──────────────────────
# I mercati crypto 15-min hanno fee DINAMICHE:
#   fee = p * (1 - p) * FEE_RATE, dove FEE_RATE = 0.0625
#   A p=0.50 → fee = 0.50 * 0.50 * 0.0625 = ~1.56% per lato = ~3.12% totale
# La maggior parte degli ALTRI mercati rimane fee-free.
# Per arbitraggio pair: dobbiamo comprare YES + NO, entrambi taker = doppia fee.
FEE_RATE_CRYPTO_15MIN = 0.0625  # Taker fee rate per mercati crypto 15-min
FEE_BUFFER_DEFAULT = 0.005      # Buffer conservativo per mercati senza fee
# Minimo profitto per trade (in $ per $1 investito)
MIN_PROFIT_PER_DOLLAR = 0.005   # 0.5% minimo


def dynamic_fee(price: float, is_crypto_short: bool = False) -> float:
    """Calcola la fee dinamica per un singolo lato di un trade.

    Formula Polymarket: fee = p * (1 - p) * FEE_RATE
    Dove p e' il prezzo/probabilita' dello share acquistato.

    Per mercati non-crypto o a lungo termine: fee ~= 0 (fee-free).
    """
    # v8.0: Fee reale per TUTTI i mercati (Becker: fee a price < 0.50 = 14.6%)
    # Formula Polymarket: p * (1-p) * 0.0625 per side (taker fee)
    return price * (1.0 - price) * FEE_RATE_CRYPTO_15MIN


@dataclass
class GabagoolOpportunity:
    """Un'opportunita' di arbitraggio identificata."""
    type: str  # "pair" | "combinatorial" | "neg_risk_set"
    markets: list[Market]
    profit_per_dollar: float  # Profitto garantito per dollaro investito
    total_cost: float  # Costo per comprare la combinazione
    guaranteed_payout: float  # Payout garantito alla risoluzione
    action: str
    details: dict = field(default_factory=dict)

    @property
    def edge(self) -> float:
        return self.profit_per_dollar


@dataclass
class OrderBookQuote:
    """Prezzi reali dall'order book CLOB."""
    token_id: str
    best_ask: float  # Prezzo piu' basso a cui possiamo COMPRARE
    best_bid: float  # Prezzo piu' alto a cui possiamo VENDERE
    ask_size: float  # Quantita' disponibile al best ask
    bid_size: float
    mid: float


class GabagoolStrategy:
    """
    Strategia di arbitraggio puro alla gabagool22.

    Non predice nulla. Trova solo situazioni dove il profitto e' matematicamente
    garantito, indipendentemente dall'esito del mercato.
    """

    # Pattern per riconoscere mercati crypto a breve termine (con fee dinamiche)
    _CRYPTO_SHORT_PATTERN = re.compile(
        r"\b(?:btc|bitcoin|eth|ethereum|sol|solana|xrp|ripple)\b.*"
        r"(?:5[\s-]?min|15[\s-]?min|up\s+or\s+down)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        min_profit: float = MIN_PROFIT_PER_DOLLAR,
    ):
        self.api = api
        self.risk = risk
        self.min_profit = min_profit
        self._opportunities_found = 0
        self._trades_executed = 0
        self._price_cache: dict[str, OrderBookQuote] = {}
        self._cache_time: float = 0
        self._recently_traded: dict[str, float] = {}

    async def scan(self, shared_markets: list[Market] | None = None) -> list[GabagoolOpportunity]:
        """Scansiona mercati per opportunita' di arbitraggio garantito."""
        markets = shared_markets or self.api.fetch_markets(limit=200)
        if not markets:
            return []

        opps: list[GabagoolOpportunity] = []

        # Invalida cache prezzi ogni 30 secondi
        now = time.time()
        if now - self._cache_time > 30:
            self._price_cache.clear()
            self._cache_time = now

        # Pulisci _recently_traded: rimuovi entry piu' vecchie di 1 ora
        stale = [k for k, t in self._recently_traded.items() if now - t > 3600]
        for k in stale:
            del self._recently_traded[k]

        # 1. Arbitraggio a coppie: YES + NO < 1.00
        pair_opps = self._find_pair_arbitrage(markets)
        opps.extend(pair_opps)

        # 2. Frank-Wolfe combinatorio: mercati con soglie correlate
        combo_opps = self._find_combinatorial_arbitrage(markets)
        opps.extend(combo_opps)

        # 3. NegRisk set: mercati mutuamente esclusivi (es. elezioni)
        neg_opps = self._find_neg_risk_arbitrage(markets)
        opps.extend(neg_opps)

        opps.sort(key=lambda o: o.profit_per_dollar, reverse=True)
        self._opportunities_found += len(opps)

        if opps:
            logger.info(
                f"[GABAGOOL] Scan {len(markets)} mercati → "
                f"{len(opps)} arbitraggi (pair:{len(pair_opps)} "
                f"combo:{len(combo_opps)} neg:{len(neg_opps)}) "
                f"migliore: {opps[0].profit_per_dollar:.4f}/$ "
                f"tipo={opps[0].type}"
            )
        else:
            logger.info(
                f"[GABAGOOL] Scan {len(markets)} mercati → 0 arbitraggi "
                f"(pair:{len(pair_opps)} combo:{len(combo_opps)} neg:{len(neg_opps)})"
            )

        return opps

    # ── 1. Arbitraggio a coppie YES+NO ──

    def _find_pair_arbitrage(self, markets: list[Market]) -> list[GabagoolOpportunity]:
        """
        Trova mercati dove YES_ask + NO_ask < 1.00 (dopo fee).
        Comprare entrambi garantisce $1.00 alla risoluzione.

        Usa get_price() dall'API CLOB per i prezzi ask reali
        (piu' affidabile di get_order_book che puo' essere stale).
        """
        opps = []

        for m in markets:
            # Cooldown: non tradare lo stesso mercato entro 5 minuti
            if m.id in self._recently_traded:
                if time.time() - self._recently_traded[m.id] < 300:
                    continue

            # Skip mercati con liquidita' troppo bassa (spread troppo largo)
            if m.liquidity < 1000:
                continue

            # Prezzi dalla Gamma API (mid-price, gia' disponibili)
            p_yes = m.prices.get("yes", 0.5)
            p_no = m.prices.get("no", 0.5)
            gamma_total = p_yes + p_no

            # Quick filter: se il mid-price gia' somma > 0.98, impossibile arb
            if gamma_total > 0.98:
                continue

            # Rileva se e' un mercato crypto short-term (fee dinamiche ~3%)
            is_crypto_short = bool(self._CRYPTO_SHORT_PATTERN.search(m.question))

            # Fetch prezzi ASK reali dal CLOB
            yes_quote = self._get_clob_price(m.tokens["yes"])
            no_quote = self._get_clob_price(m.tokens["no"])

            if not yes_quote or not no_quote:
                continue

            # Costo reale per comprare entrambi (YES + NO)
            total_ask = yes_quote.best_ask + no_quote.best_ask
            # Fee dinamiche: calcolate su ogni lato separatamente
            fee_yes = dynamic_fee(yes_quote.best_ask, is_crypto_short)
            fee_no = dynamic_fee(no_quote.best_ask, is_crypto_short)
            cost_with_fee = total_ask + fee_yes + fee_no

            if cost_with_fee < 1.0:
                profit = 1.0 - cost_with_fee
                # v8.0: Profit/fee ratio gate — rifiuta se fee > 50% del profitto
                total_fees = fee_yes + fee_no
                if total_fees > 0.50 * profit:
                    continue  # Fee mangiano troppo margine
                if profit >= self.min_profit:
                    # Verifica che ci sia abbastanza liquidita'
                    max_shares = min(yes_quote.ask_size, no_quote.ask_size)
                    if max_shares < 5:  # Almeno 5 shares disponibili
                        continue

                    opps.append(GabagoolOpportunity(
                        type="pair",
                        markets=[m],
                        profit_per_dollar=profit / cost_with_fee,
                        total_cost=cost_with_fee,
                        guaranteed_payout=1.0,
                        action=(
                            f"BUY YES@{yes_quote.best_ask:.4f} + "
                            f"NO@{no_quote.best_ask:.4f} = "
                            f"{total_ask:.4f} + fee → profitto "
                            f"${profit:.4f}/share"
                        ),
                        details={
                            "yes_ask": yes_quote.best_ask,
                            "no_ask": no_quote.best_ask,
                            "max_shares": max_shares,
                            "yes_token": m.tokens["yes"],
                            "no_token": m.tokens["no"],
                        },
                    ))

        return opps

    # ── 2. Frank-Wolfe Combinatorio ──

    def _find_combinatorial_arbitrage(self, markets: list[Market]) -> list[GabagoolOpportunity]:
        """
        Arbitraggio combinatorio tra mercati con soglie correlate.

        Esempio: mercati BTC con soglie $65k, $68k, $70k, $72k.
        Se P(BTC>65k) dovrebbe essere >= P(BTC>68k) >= P(BTC>70k) >= P(BTC>72k),
        ma i prezzi di mercato violano questa relazione, possiamo costruire
        un portafoglio che profita indipendentemente dal prezzo finale di BTC.

        Algoritmo semplificato di Frank-Wolfe:
        1. Raggruppa mercati per asset e tipo (above/below)
        2. Ordina per soglia
        3. Cerca coppie dove la relazione di dominanza e' violata
        4. Calcola il portafoglio ottimale con programmazione lineare leggera
        """
        opps = []

        for asset in ["btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp"]:
            threshold_markets = self._extract_threshold_markets(markets, asset)
            if len(threshold_markets) < 2:
                continue

            # Ordina per soglia crescente
            threshold_markets.sort(key=lambda x: x[0])

            # Cerca violazioni di dominanza stocastica (usando prezzi CLOB ask)
            for i in range(len(threshold_markets) - 1):
                for j in range(i + 1, len(threshold_markets)):
                    low_thresh, low_mkt, low_type = threshold_markets[i]
                    high_thresh, high_mkt, high_type = threshold_markets[j]

                    # Solo mercati dello stesso tipo (entrambi "above" o "below")
                    if low_type != high_type:
                        continue

                    # Usa CLOB ask price invece di mid-price
                    low_quote = self._get_clob_price(low_mkt.tokens["yes"])
                    high_quote = self._get_clob_price(high_mkt.tokens["yes"])
                    if not low_quote or not high_quote:
                        continue

                    p_low = low_quote.best_ask
                    p_high = high_quote.best_ask

                    # Fee buffer per coppie combinatorie (2 lati)
                    fee_buf = dynamic_fee(p_low, False) + dynamic_fee(1.0 - p_high, False)

                    if high_type == "above":
                        # P(asset > soglia_bassa) DEVE essere >= P(asset > soglia_alta)
                        # Se p_high > p_low → violazione!
                        if p_high > p_low + fee_buf:
                            edge = p_high - p_low - fee_buf
                            if edge >= self.min_profit:
                                opps.append(self._build_combo_opp(
                                    low_mkt, high_mkt, low_thresh, high_thresh,
                                    p_low, p_high, edge, "above"
                                ))
                    else:  # "below"
                        # P(asset < soglia_alta) DEVE essere >= P(asset < soglia_bassa)
                        # Se p_low > p_high → violazione!
                        if p_low > p_high + fee_buf:
                            edge = p_low - p_high - fee_buf
                            if edge >= self.min_profit:
                                opps.append(self._build_combo_opp(
                                    high_mkt, low_mkt, high_thresh, low_thresh,
                                    p_high, p_low, edge, "below"
                                ))

            # Frank-Wolfe avanzato: cerca combinazioni di 3+ mercati
            if len(threshold_markets) >= 3:
                fw_opps = self._frank_wolfe_optimize(threshold_markets)
                opps.extend(fw_opps)

        return opps

    def _frank_wolfe_optimize(
        self, threshold_markets: list[tuple[float, Market, str]]
    ) -> list[GabagoolOpportunity]:
        """
        Ottimizzazione Frank-Wolfe semplificata per arbitraggio combinatorio.

        Cerca portafogli di 3+ mercati dove il profitto e' garantito
        in TUTTI gli scenari possibili (prezzo finale sopra/sotto ogni soglia).

        Algoritmo:
        1. Definisci gli scenari (intervalli di prezzo tra soglie)
        2. Per ogni scenario, calcola il payoff di ogni mercato
        3. Cerca combinazioni dove il payoff minimo e' > 0
        """
        opps = []

        # Filtra solo mercati "above" per semplicita'
        above_markets = [(t, m) for t, m, tp in threshold_markets if tp == "above"]
        if len(above_markets) < 3:
            return opps

        # Limita a 8 mercati per performance (combinazioni esplodono)
        above_markets = above_markets[:8]

        thresholds = [t for t, _ in above_markets]
        mkts = [m for _, m in above_markets]

        # Scenari: prezzo finale in ogni intervallo tra soglie
        # Scenario 0: prezzo < soglia_min
        # Scenario 1: soglia_0 <= prezzo < soglia_1
        # ...
        # Scenario N: prezzo >= soglia_max
        n = len(thresholds)
        n_scenarios = n + 1

        # Per ogni mercato i con soglia T_i:
        # - YES paga 1 se prezzo >= T_i, 0 altrimenti
        # - NO paga 1 se prezzo < T_i, 0 altrimenti
        # Il costo di YES e' p_yes_i, il costo di NO e' p_no_i

        # Cerca triplette dove possiamo costruire un portafoglio risk-free
        for combo_size in [3, 4]:
            if len(above_markets) < combo_size:
                break

            for combo in combinations(range(n), combo_size):
                # Prova la strategia: compra NO sui mercati con soglia bassa
                # e YES sui mercati con soglia alta
                # (sfrutta la violazione di dominanza stocastica)

                combo_mkts = [mkts[i] for i in combo]
                combo_thresh = [thresholds[i] for i in combo]
                # Usa CLOB ask prices invece di mid-prices
                combo_quotes = [self._get_clob_price(mkts[i].tokens["yes"]) for i in combo]
                if not all(combo_quotes):
                    continue
                prices_yes = [q.best_ask for q in combo_quotes]
                prices_no = [1.0 - p for p in prices_yes]

                # Strategia: compra YES sul mercato piu' caro (soglia bassa)
                # e NO sul mercato piu' economico (soglia alta)
                # In un portafoglio ben costruito, almeno una posizione paga

                # Calcola payoff in ogni scenario
                best_strat = self._find_best_combo_strategy(
                    combo_thresh, prices_yes, combo_mkts
                )
                if best_strat:
                    opps.append(best_strat)

        return opps

    def _find_best_combo_strategy(
        self,
        thresholds: list[float],
        prices_yes: list[float],
        mkts: list[Market],
    ) -> GabagoolOpportunity | None:
        """
        Cerca la migliore strategia combinatoria per un set di mercati.

        Per N mercati con soglie ordinate T_1 < T_2 < ... < T_N,
        ci sono N+1 scenari possibili. Per ogni combinazione di posizioni
        (YES/NO/SKIP per ogni mercato), calcola il payoff minimo.
        Se il payoff minimo > costo totale, abbiamo un arbitraggio.
        """
        n = len(thresholds)
        n_scenarios = n + 1

        # Genera strategie candidate (non tutte 3^N, solo le piu' promettenti)
        # Strategia tipo gabagool: compra il lato sottovalutato
        strategies = []

        # Strategia 1: spread — compra YES basso, NO alto
        if n >= 2:
            strategies.append([(1, 0)] * 1 + [(0, 0)] * (n - 2) + [(0, 1)] * 1)

        # Strategia 2: butterfly — YES basso + NO alto + YES medio
        if n >= 3:
            mid = n // 2
            s = [(0, 0)] * n
            s[0] = (1, 0)  # YES sul piu' basso
            s[mid] = (0, 1)  # NO sul medio
            s[-1] = (1, 0)  # YES sul piu' alto
            strategies.append(s)

        # Strategia 3: tutte le violazioni dirette
        for i in range(n - 1):
            p_low = prices_yes[i]
            p_high = prices_yes[i + 1]
            if p_high > p_low:  # Violazione di dominanza
                s = [(0, 0)] * n
                s[i] = (1, 0)    # BUY YES basso (economico)
                s[i + 1] = (0, 1)  # BUY NO alto (economico perche' YES e' caro)
                strategies.append(s)

        best_opp = None
        best_profit = 0

        for strategy in strategies:
            # Calcola costo totale (con fee dinamiche stimate)
            total_cost = 0
            for i, (buy_yes, buy_no) in enumerate(strategy):
                if buy_yes:
                    p = prices_yes[i]
                    total_cost += p + dynamic_fee(p, is_crypto_short=False)
                if buy_no:
                    p = 1.0 - prices_yes[i]
                    total_cost += p + dynamic_fee(p, is_crypto_short=False)

            if total_cost <= 0:
                continue

            # Calcola payoff in ogni scenario
            min_payoff = float('inf')
            for scenario in range(n_scenarios):
                payoff = 0
                for i, (buy_yes, buy_no) in enumerate(strategy):
                    # Nel scenario s, il prezzo e' nell'intervallo s
                    # Mercato i con soglia thresholds[i]:
                    # YES paga 1 se scenario > i (prezzo >= soglia_i)
                    # NO paga 1 se scenario <= i (prezzo < soglia_i)
                    if buy_yes and scenario > i:
                        payoff += 1.0
                    if buy_no and scenario <= i:
                        payoff += 1.0
                min_payoff = min(min_payoff, payoff)

            profit = min_payoff - total_cost
            if profit > best_profit and profit >= self.min_profit:
                best_profit = profit

                # Costruisci la descrizione
                actions = []
                for i, (buy_yes, buy_no) in enumerate(strategy):
                    if buy_yes:
                        actions.append(f"YES@{prices_yes[i]:.3f}(>{thresholds[i]:,.0f})")
                    if buy_no:
                        actions.append(f"NO@{1-prices_yes[i]:.3f}(<{thresholds[i]:,.0f})")

                best_opp = GabagoolOpportunity(
                    type="combinatorial",
                    markets=[m for m, (by, bn) in zip(mkts, strategy) if by or bn],
                    profit_per_dollar=profit / total_cost,
                    total_cost=total_cost,
                    guaranteed_payout=min_payoff,
                    action=f"COMBO: {' + '.join(actions)} → min_pay={min_payoff:.2f} cost={total_cost:.3f}",
                    details={
                        "strategy": strategy,
                        "thresholds": thresholds,
                        "min_payoff": min_payoff,
                    },
                )

        return best_opp

    # ── 3. NegRisk Set Arbitrage ──

    def _find_neg_risk_arbitrage(self, markets: list[Market]) -> list[GabagoolOpportunity]:
        """
        Trova arbitraggi nei NegRisk set (mercati mutuamente esclusivi).

        In un evento con N outcome mutuamente esclusivi (es. "Chi vince le elezioni?"),
        esattamente un outcome sara' YES. Quindi la somma di tutti i YES dovrebbe = 1.0.
        Se la somma < 1.0 (dopo fee), comprare YES su tutti gli outcome e' un arbitraggio.
        """
        opps = []

        # Raggruppa mercati per condition_id comune (stesso evento)
        events: dict[str, list[Market]] = {}
        for m in markets:
            # I mercati NegRisk condividono il condition_id
            cid = m.condition_id
            if cid:
                events.setdefault(cid, []).append(m)

        for cid, event_markets in events.items():
            if len(event_markets) < 2:
                continue

            # Fetch prezzi ASK reali dal CLOB per ogni outcome
            quotes: list[tuple[Market, OrderBookQuote]] = []
            for m in event_markets:
                q = self._get_clob_price(m.tokens["yes"])
                if q:
                    quotes.append((m, q))

            if len(quotes) < 2:
                continue

            # Calcola somma dei prezzi YES ask + fee dinamiche
            total_yes = sum(q.best_ask for _, q in quotes)
            total_fee = sum(
                dynamic_fee(q.best_ask, is_crypto_short=False)
                for _, q in quotes
            )
            cost_with_fee = total_yes + total_fee
            valid_markets = [m for m, _ in quotes]

            if cost_with_fee < 1.0:
                profit = 1.0 - cost_with_fee
                if profit >= self.min_profit:
                    market_desc = " + ".join(
                        f"YES@{q.best_ask:.3f}"
                        for _, q in quotes[:4]
                    )
                    if len(quotes) > 4:
                        market_desc += f" +{len(quotes) - 4} altri"

                    opps.append(GabagoolOpportunity(
                        type="neg_risk_set",
                        markets=valid_markets,
                        profit_per_dollar=profit / cost_with_fee,
                        total_cost=cost_with_fee,
                        guaranteed_payout=1.0,
                        action=f"NEG_RISK: {market_desc} = {total_yes:.3f} + fee < 1.00",
                        details={"n_outcomes": len(valid_markets)},
                    ))

        return opps

    # ── Helper: prezzi CLOB ──

    def _get_clob_price(self, token_id: str) -> OrderBookQuote | None:
        """Fetch prezzo ask/bid reale dal CLOB API."""
        if token_id in self._price_cache:
            return self._price_cache[token_id]

        if not self.api.clob:
            return None

        try:
            price_data = self.api.clob.get_price(token_id, "BUY")
            sell_data = self.api.clob.get_price(token_id, "SELL")

            ask = float(price_data) if price_data else 0
            bid = float(sell_data) if sell_data else 0

            if ask <= 0:
                return None

            # Prova a ottenere la size reale dall'order book
            ask_size = 100  # default fallback
            bid_size = 100
            try:
                book = self.api.clob.get_order_book(token_id)
                if book and book.asks:
                    ask_size = float(book.asks[0].size)
                if book and book.bids:
                    bid_size = float(book.bids[0].size)
            except Exception:
                pass  # Usa default se get_order_book non disponibile

            quote = OrderBookQuote(
                token_id=token_id,
                best_ask=ask,
                best_bid=bid,
                ask_size=ask_size,
                bid_size=bid_size,
                mid=(ask + bid) / 2 if bid > 0 else ask,
            )
            self._price_cache[token_id] = quote
            return quote

        except Exception as e:
            logger.debug(f"[GABAGOOL] Errore fetch prezzo {token_id[:16]}: {e}")
            return None

    def _extract_threshold_markets(
        self, markets: list[Market], asset: str
    ) -> list[tuple[float, Market, str]]:
        """
        Estrai mercati con soglie di prezzo per un asset.
        Ritorna (soglia, mercato, tipo) dove tipo e' "above" o "below".
        """
        results = []
        asset_lower = asset.lower()

        patterns_above = [
            rf"(?:will\s+)?(?:{asset_lower}|{asset}).*(?:above|over|reach|hit|break|close above|end above|>=?)\s*\$?([\d,]+(?:\.\d+)?)",
            rf"\$?([\d,]+(?:\.\d+)?)\s*(?:or more|or higher|or above).*(?:{asset_lower}|{asset})",
        ]
        patterns_below = [
            rf"(?:will\s+)?(?:{asset_lower}|{asset}).*(?:below|under|fall|drop|close below|end below|<=?)\s*\$?([\d,]+(?:\.\d+)?)",
            rf"\$?([\d,]+(?:\.\d+)?)\s*(?:or less|or lower|or below).*(?:{asset_lower}|{asset})",
        ]

        for m in markets:
            q = m.question.lower()
            if asset_lower not in q:
                # Prova aliases
                aliases = {
                    "btc": ["bitcoin"], "eth": ["ethereum"],
                    "sol": ["solana"], "xrp": ["ripple"],
                    "bitcoin": ["btc"], "ethereum": ["eth"],
                    "solana": ["sol"], "ripple": ["xrp"],
                }
                found = False
                for alias in aliases.get(asset_lower, []):
                    if alias in q:
                        found = True
                        break
                if not found:
                    continue

            # Prova patterns above
            for pattern in patterns_above:
                match = re.search(pattern, q)
                if match:
                    try:
                        threshold = float(match.group(1).replace(",", ""))
                        results.append((threshold, m, "above"))
                        break
                    except ValueError:
                        continue

            # Prova patterns below
            for pattern in patterns_below:
                match = re.search(pattern, q)
                if match:
                    try:
                        threshold = float(match.group(1).replace(",", ""))
                        results.append((threshold, m, "below"))
                        break
                    except ValueError:
                        continue

            # Pattern generico: "$XX,XXX" con asset name
            if not any(t[1].id == m.id for t in results):
                generic = re.search(r"\$?([\d,]+(?:\.\d+)?)", q)
                if generic:
                    try:
                        val = float(generic.group(1).replace(",", ""))
                        if val > 100:  # Probabilmente un prezzo crypto
                            # Cerca indizi per capire se above o below
                            if any(w in q for w in ["above", "over", "reach", "hit", "break", "up"]):
                                results.append((val, m, "above"))
                            elif any(w in q for w in ["below", "under", "fall", "drop", "down"]):
                                results.append((val, m, "below"))
                    except ValueError:
                        pass

        return results

    def _build_combo_opp(
        self, buy_yes_mkt: Market, buy_no_mkt: Market,
        thresh_yes: float, thresh_no: float,
        p_yes: float, p_no: float,
        edge: float, market_type: str
    ) -> GabagoolOpportunity:
        """Costruisci un'opportunita' combinatoria da una coppia."""
        cost = p_yes + (1.0 - p_no)
        return GabagoolOpportunity(
            type="combinatorial",
            markets=[buy_yes_mkt, buy_no_mkt],
            profit_per_dollar=edge / cost if cost > 0 else 0,
            total_cost=cost,
            guaranteed_payout=1.0,
            action=(
                f"BUY YES '{buy_yes_mkt.question[:30]}' @{p_yes:.3f} + "
                f"BUY NO '{buy_no_mkt.question[:30]}' @{1-p_no:.3f} | "
                f"Violazione dominanza: P(>{thresh_yes:,.0f})={p_yes:.3f} "
                f"vs P(>{thresh_no:,.0f})={p_no:.3f}"
            ),
            details={
                "thresh_yes": thresh_yes,
                "thresh_no": thresh_no,
                "violation": p_no - p_yes,
            },
        )

    # ── Esecuzione ──

    async def execute(self, opp: GabagoolOpportunity, paper: bool = True) -> bool:
        """Esegue un'opportunita' di arbitraggio."""
        if opp.profit_per_dollar < self.min_profit:
            return False

        # Calcola size basata sul profitto atteso
        # Per arbitraggio puro, possiamo investire di piu' (rischio ~0)
        max_investment = min(
            self.risk.config.max_bet_size * 2,  # Raddoppia per arb puro
            self.risk.capital * 0.10,  # Max 10% capitale per singolo arb
        )

        size = min(max_investment, 50.0)  # Cap a $50 per sicurezza iniziale

        allowed, reason = self.risk.can_trade(STRATEGY_NAME, size, side="BUY_YES", market_id=opp.markets[0].id)
        if not allowed:
            logger.info(f"[GABAGOOL] Trade bloccato: {reason}")
            return False

        if opp.type == "pair":
            return await self._execute_pair(opp, size, paper)
        elif opp.type == "combinatorial":
            return await self._execute_combo(opp, size, paper)
        elif opp.type == "neg_risk_set":
            return await self._execute_neg_risk(opp, size, paper)

        return False

    async def _execute_pair(
        self, opp: GabagoolOpportunity, size: float, paper: bool
    ) -> bool:
        """Esegui arbitraggio a coppie (compra YES + NO)."""
        m = opp.markets[0]
        details = opp.details

        # Calcola numero uguale di shares su entrambi i lati
        yes_ask = details["yes_ask"]
        no_ask = details["no_ask"]
        cost_per_share = yes_ask + no_ask
        if cost_per_share <= 0:
            return False
        shares = size / cost_per_share
        yes_dollars = shares * yes_ask
        no_dollars = shares * no_ask

        trade = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id=m.id,
            token_id=details["yes_token"],
            side="BUY_YES",
            size=size,
            price=details["yes_ask"],
            edge=opp.profit_per_dollar,
            reason=f"[GABAGOOL] PAIR: {opp.action}",
        )

        if paper:
            logger.info(f"[PAPER] GABAGOOL PAIR: {opp.action} size=${size:.2f}")
            self.risk.open_trade(trade)
            # Arbitraggio puro: profitto garantito
            pnl = size * opp.profit_per_dollar
            self.risk.close_trade(details["yes_token"], won=True, pnl=pnl)
        else:
            # Compra entrambi i lati — ordine critico!
            # v5.9.4: Protezione atomica — se il secondo lato fallisce,
            # vendiamo il primo per evitare esposizione direzionale.
            r1 = self.api.buy_market(details["yes_token"], yes_dollars)
            if r1:
                # v7.4: Aggiorna prezzo con fill reale dal CLOB
                if isinstance(r1, dict) and r1.get("_fill_price"):
                    trade.price = r1["_fill_price"]
                r2 = self.api.buy_market(details["no_token"], no_dollars)
                if r2:
                    self.risk.open_trade(trade)
                    logger.info(
                        f"[LIVE] GABAGOOL PAIR eseguito: YES+NO su {m.id} "
                        f"shares={shares:.2f} profitto atteso "
                        f"${size * opp.profit_per_dollar:.2f}"
                    )
                else:
                    # ROLLBACK: vendi il YES appena comprato
                    logger.warning(
                        f"[LIVE] GABAGOOL: NO fallito su {m.id} — "
                        f"ROLLBACK: vendita YES per evitare esposizione"
                    )
                    try:
                        if shares > 0:
                            rollback_ok = self.api.smart_sell(
                                details["yes_token"], shares,
                                current_price=details["yes_ask"],
                                timeout_sec=10.0,
                                fallback_market=True,
                            )
                            if rollback_ok:
                                logger.info(f"[LIVE] GABAGOOL ROLLBACK riuscito su {m.id}")
                            else:
                                logger.error(
                                    f"[LIVE] GABAGOOL ROLLBACK FALLITO su {m.id}! "
                                    f"Posizione YES sbilanciata — richiede intervento manuale"
                                )
                    except Exception as e:
                        logger.error(f"[LIVE] GABAGOOL ROLLBACK errore: {e}", exc_info=True)

        self._recently_traded[m.id] = time.time()
        self._trades_executed += 1
        return True

    async def _execute_combo(
        self, opp: GabagoolOpportunity, size: float, paper: bool
    ) -> bool:
        """Esegui arbitraggio combinatorio (multi-mercato)."""
        if not opp.markets:
            return False

        primary = opp.markets[0]
        per_market = size / len(opp.markets)

        trade = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id="|".join(m.id for m in opp.markets),
            token_id=primary.tokens["yes"],
            side="BUY_YES",
            size=size,
            price=primary.prices.get("yes", 0.5),
            edge=opp.profit_per_dollar,
            reason=f"[GABAGOOL] COMBO: {opp.action}",
        )

        if paper:
            logger.info(f"[PAPER] GABAGOOL COMBO: {opp.action} size=${size:.2f}")
            self.risk.open_trade(trade)
            pnl = size * opp.profit_per_dollar
            self.risk.close_trade(primary.tokens["yes"], won=True, pnl=pnl)
        else:
            # Esegui ogni lato della combinazione con rollback
            executed: list[tuple[str, float, float]] = []  # (token_id, shares, price)
            all_ok = True
            for m in opp.markets:
                strategy = opp.details.get("strategy", [])
                idx = next((i for i, mk in enumerate(opp.markets) if mk.id == m.id), 0)
                if idx < len(strategy):
                    buy_yes, buy_no = strategy[idx]
                else:
                    buy_yes, buy_no = 1, 0

                if buy_yes:
                    price = m.prices.get("yes", 0.5)
                    r = self.api.buy_market(m.tokens["yes"], per_market)
                    if r:
                        # v7.4: Usa fill price reale
                        fp = r.get("_fill_price", price) if isinstance(r, dict) else price
                        shares = per_market / fp if fp > 0 else 0
                        executed.append((m.tokens["yes"], shares, fp))
                    else:
                        all_ok = False
                        break
                if buy_no:
                    price = m.prices.get("no", 0.5)
                    r = self.api.buy_market(m.tokens["no"], per_market)
                    if r:
                        # v7.4: Usa fill price reale
                        fp = r.get("_fill_price", price) if isinstance(r, dict) else price
                        shares = per_market / fp if fp > 0 else 0
                        executed.append((m.tokens["no"], shares, fp))
                    else:
                        all_ok = False
                        break

            if all_ok:
                # v7.4: Aggiorna prezzo trade con fill reale del primary
                if executed:
                    trade.price = executed[0][2]
                self.risk.open_trade(trade)
            elif executed:
                # ROLLBACK: vendi tutte le posizioni gia' aperte
                logger.warning(
                    f"[LIVE] GABAGOOL COMBO fallito parziale — "
                    f"ROLLBACK di {len(executed)} posizioni"
                )
                for token_id, shares, price in executed:
                    try:
                        if shares > 0:
                            self.api.smart_sell(
                                token_id, shares,
                                current_price=price,
                                timeout_sec=10.0,
                                fallback_market=True,
                            )
                    except Exception as e:
                        logger.error(
                            f"[LIVE] GABAGOOL COMBO ROLLBACK errore {token_id[:16]}: {e}"
                        )

        for m in opp.markets:
            self._recently_traded[m.id] = time.time()
        self._trades_executed += 1
        return True

    async def _execute_neg_risk(
        self, opp: GabagoolOpportunity, size: float, paper: bool
    ) -> bool:
        """Esegui arbitraggio su NegRisk set."""
        if not opp.markets:
            return False

        primary = opp.markets[0]
        per_outcome = size / len(opp.markets)

        trade = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id=primary.condition_id or primary.id,
            token_id=primary.tokens["yes"],
            side="BUY_YES",
            size=size,
            price=primary.prices.get("yes", 0.5),
            edge=opp.profit_per_dollar,
            reason=f"[GABAGOOL] NEG_RISK: {opp.action}",
        )

        if paper:
            logger.info(f"[PAPER] GABAGOOL NEG_RISK: {opp.action} size=${size:.2f}")
            self.risk.open_trade(trade)
            pnl = size * opp.profit_per_dollar
            self.risk.close_trade(primary.tokens["yes"], won=True, pnl=pnl)
        else:
            executed: list[tuple[str, float, float]] = []  # (token_id, shares, price)
            all_ok = True
            for m in opp.markets:
                price = m.prices.get("yes", 0.5)
                r = self.api.buy_market(m.tokens["yes"], per_outcome)
                if r:
                    # v7.4: Usa fill price reale
                    fp = r.get("_fill_price", price) if isinstance(r, dict) else price
                    shares = per_outcome / fp if fp > 0 else 0
                    executed.append((m.tokens["yes"], shares, fp))
                else:
                    all_ok = False
                    break
            if all_ok:
                # v7.4: Aggiorna prezzo trade con fill reale del primary
                if executed:
                    trade.price = executed[0][2]
                self.risk.open_trade(trade)
            elif executed:
                # ROLLBACK: vendi tutte le posizioni gia' aperte
                logger.warning(
                    f"[LIVE] GABAGOOL NEG_RISK fallito parziale — "
                    f"ROLLBACK di {len(executed)} posizioni"
                )
                for token_id, shares, price in executed:
                    try:
                        if shares > 0:
                            self.api.smart_sell(
                                token_id, shares,
                                current_price=price,
                                timeout_sec=10.0,
                                fallback_market=True,
                            )
                    except Exception as e:
                        logger.error(
                            f"[LIVE] GABAGOOL NEG_RISK ROLLBACK errore {token_id[:16]}: {e}"
                        )

        for m in opp.markets:
            self._recently_traded[m.id] = time.time()
        self._trades_executed += 1
        return True
