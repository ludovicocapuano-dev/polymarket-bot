"""
Strategia 1: Arbitraggio Multi-Source — v4.1
=============================================
Combina arbitraggio interno Polymarket + cross-platform via ArbBets + Dome.

Fonti:
1. Interno Polymarket: YES + NO < 1.0, cross-market (BTC soglie), low-liq mispricing
2. ArbBets API: 80-100 arb/giorno tra Polymarket, Kalshi, Opinion (ROI ~4.87%)
3. Dome API: layer unificato cross-platform (Polymarket, Kalshi, PredictIt)

Il cross-platform e' dove si fanno i profitti veri ($40M documentati 2024-2025):
se Polymarket dice "BTC > 100k" = 65% e Kalshi dice = 58%, compri YES su Kalshi
e NO su Polymarket per profitto quasi risk-free.
"""

import logging
import re
import time
from dataclasses import dataclass

from utils.polymarket_api import Market, PolymarketAPI
from utils.risk_manager import RiskManager, Trade
from utils.arbbets_feed import ArbBetsFeed, CrossPlatformArb
from utils.dome_feed import DomeFeed, DomeArb

logger = logging.getLogger(__name__)

STRATEGY_NAME = "arbitrage"


@dataclass
class ArbOpportunity:
    """Un'opportunita' di arbitraggio identificata."""
    type: str  # "simple" | "cross_market" | "cross_platform"
    markets: list[Market]
    edge: float  # Profitto atteso in percentuale
    action: str  # Descrizione dell'azione da prendere
    confidence: float
    cross_platform_arb: CrossPlatformArb | None = None  # Dati ArbBets (se cross-platform)

    @property
    def is_profitable(self) -> bool:
        return self.edge > 0.01  # Almeno 1% dopo fees


class ArbitrageStrategy:
    """
    Identifica e sfrutta opportunita' di arbitraggio su Polymarket.

    Tipi di arbitraggio:
    1. Semplice: YES + NO < $1.00 sullo stesso mercato
    2. Cross-market: discrepanze logiche tra mercati correlati
    3. Temporale: mercati sullo stesso evento con scadenze diverse
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        arbbets: ArbBetsFeed | None = None,
        dome: DomeFeed | None = None,
        min_edge: float = 0.01,
        fee_rate: float = 0.005,  # Fee maker (piu' bassa del taker)
    ):
        self.api = api
        self.risk = risk
        self.arbbets = arbbets
        self.dome = dome
        self.min_edge = min_edge
        self.fee_rate = fee_rate
        self._opportunities_found = 0
        self._trades_executed = 0
        self._cross_platform_found = 0
        self._markets: list[Market] = []  # Cache mercati condivisi

    async def scan(self, shared_markets: list[Market] | None = None) -> list[ArbOpportunity]:
        """
        Scansiona tutti i mercati attivi per opportunita' di arbitraggio.
        Ritorna una lista di opportunita' ordinate per edge.
        Accetta mercati pre-fetchati per evitare chiamate API duplicate.
        """
        opportunities = []

        # Usa mercati condivisi se disponibili, altrimenti fetch
        markets = shared_markets or self.api.fetch_markets(limit=200)
        self._markets = markets

        if not markets:
            logger.info("[ARB] Scan: 0 mercati disponibili — nessuna scansione")
            return []

        # 1. Arbitraggio semplice: YES + NO < 1.0
        simple_opps = self._find_simple_arbitrage(markets)
        opportunities.extend(simple_opps)

        # 2. Cross-market: mercati correlati con prezzi incoerenti
        cross_opps = self._find_cross_market_arbitrage(markets)
        opportunities.extend(cross_opps)

        # 3. Multi-outcome: mercati con piu' outcomes dove la somma != 1.0
        multi_opps = self._find_multi_outcome_arbitrage(markets)
        opportunities.extend(multi_opps)

        # 4. Cross-platform via ArbBets: Polymarket vs Kalshi vs Opinion
        xplat_opps = self._find_cross_platform_arbitrage(markets)
        opportunities.extend(xplat_opps)

        # 5. Cross-platform via Dome: layer unificato Polymarket + Kalshi + PredictIt
        dome_opps = self._find_dome_arbitrage(markets)
        opportunities.extend(dome_opps)

        self._cross_platform_found += len(xplat_opps) + len(dome_opps)

        # Ordina per edge decrescente
        opportunities.sort(key=lambda o: o.edge, reverse=True)
        self._opportunities_found += len(opportunities)

        if opportunities:
            xplat_note = f", xplat: {len(xplat_opps)}" if self.arbbets else ""
            dome_note = f", dome: {len(dome_opps)}" if self.dome else ""
            logger.info(
                f"[ARB] Scan {len(markets)} mercati → "
                f"{len(opportunities)} opportunita' "
                f"(migliore: {opportunities[0].edge:.4f} — {opportunities[0].type}"
                f"{xplat_note}{dome_note})"
            )
        else:
            xplat_note = f", xplat: {len(xplat_opps)}" if self.arbbets else ""
            dome_note = f", dome: {len(dome_opps)}" if self.dome else ""
            logger.info(
                f"[ARB] Scan {len(markets)} mercati → 0 opportunita' "
                f"(simple: {len(simple_opps)}, cross: {len(cross_opps)}, "
                f"multi: {len(multi_opps)}{xplat_note}{dome_note})"
            )

        return opportunities

    def _find_simple_arbitrage(self, markets: list[Market]) -> list[ArbOpportunity]:
        """
        Trova mercati dove YES + NO < 1.0 (dopo fee).
        Comprare entrambi garantisce un profitto.
        Include anche mercati con spread anomalo (YES + NO > 1.02) come
        segnale di mispricing da sfruttare vendendo il lato sopravvalutato.
        """
        opps = []

        for m in markets:
            price_yes = m.prices.get("yes", 0.5)
            price_no = m.prices.get("no", 0.5)
            total = price_yes + price_no

            # Caso 1: YES + NO < 1.0 — compra entrambi
            cost_with_fees = total * (1 + self.fee_rate)
            if cost_with_fees < 1.0:
                edge = 1.0 - cost_with_fees
                if edge > self.min_edge:
                    opp = ArbOpportunity(
                        type="simple",
                        markets=[m],
                        edge=edge,
                        action=(
                            f"Compra YES@{price_yes:.4f} + NO@{price_no:.4f} "
                            f"= {total:.4f} + fee = {cost_with_fees:.4f} < 1.00"
                        ),
                        confidence=0.95,
                    )
                    opps.append(opp)

            # Caso 2: YES + NO > 1.0 — uno dei due lati e' sopravvalutato
            # Segnale di mispricing: il lato piu' caro probabilmente scende
            elif total > 1.02:
                edge = (total - 1.0) * 0.4 - self.fee_rate  # Edge stimato
                if edge > self.min_edge:
                    overpriced_side = "NO" if price_no > (1 - price_yes) + 0.01 else "YES"
                    opp = ArbOpportunity(
                        type="simple_overpriced",
                        markets=[m],
                        edge=edge,
                        action=(
                            f"Mispricing: YES+NO={total:.4f} > 1.0 — "
                            f"{overpriced_side} sopravvalutato"
                        ),
                        confidence=0.70,
                    )
                    opps.append(opp)

        return opps

    def _find_multi_outcome_arbitrage(self, markets: list[Market]) -> list[ArbOpportunity]:
        """
        Trova mercati dove la somma di YES + NO devia significativamente da 1.0
        anche in modo piu' sottile (mispricing da liquidita' bassa).
        Questi sono spesso mercati con poca attenzione dove i prezzi
        non vengono aggiornati frequentemente.
        """
        opps = []

        for m in markets:
            price_yes = m.prices.get("yes", 0.5)
            price_no = m.prices.get("no", 0.5)
            total = price_yes + price_no
            deviation = abs(total - 1.0)

            # Mercati con bassa liquidita' e deviazione significativa
            if deviation > 0.015 and m.liquidity < 20000 and m.volume > 500:
                edge = deviation * 0.5 - self.fee_rate
                if edge > self.min_edge * 0.8:  # Soglia leggermente piu' bassa
                    side = "YES" if price_yes < (1 - price_no) else "NO"
                    opp = ArbOpportunity(
                        type="low_liquidity_mispricing",
                        markets=[m],
                        edge=max(edge, 0.005),
                        action=(
                            f"Low-liq mispricing: YES+NO={total:.4f} "
                            f"dev={deviation:.4f} liq=${m.liquidity:,.0f} — BUY {side}"
                        ),
                        confidence=0.60,
                    )
                    opps.append(opp)

        return opps

    def _find_cross_market_arbitrage(
        self, markets: list[Market]
    ) -> list[ArbOpportunity]:
        """
        Trova discrepanze logiche tra mercati correlati.
        Es: "BTC > 100k" e "BTC > 95k" devono avere prezzi coerenti.
        """
        opps = []

        # Raggruppa mercati per asset/tema
        btc_price_markets = self._extract_price_threshold_markets(markets, "btc")
        eth_price_markets = self._extract_price_threshold_markets(markets, "eth")

        for asset_markets in [btc_price_markets, eth_price_markets]:
            if len(asset_markets) < 2:
                continue

            # Ordina per soglia di prezzo
            sorted_markets = sorted(asset_markets, key=lambda x: x[0])

            # Confronta coppie: se soglia_alta > soglia_bassa,
            # allora P(sopra alta) <= P(sopra bassa)
            for i in range(len(sorted_markets) - 1):
                low_threshold, low_market = sorted_markets[i]
                high_threshold, high_market = sorted_markets[i + 1]

                prob_low = low_market.prices.get("yes", 0.5)
                prob_high = high_market.prices.get("yes", 0.5)

                # Discrepanza: la probabilita' di superare una soglia piu' alta
                # NON puo' essere maggiore della probabilita' di superare una piu' bassa
                if prob_high > prob_low + self.min_edge:
                    edge = prob_high - prob_low - self.fee_rate * 2
                    if edge > self.min_edge:
                        opp = ArbOpportunity(
                            type="cross_market",
                            markets=[low_market, high_market],
                            edge=edge,
                            action=(
                                f"BUY YES '{low_market.question}' @{prob_low:.4f} + "
                                f"BUY NO '{high_market.question}' @{1 - prob_high:.4f}"
                            ),
                            confidence=0.85,
                        )
                        opps.append(opp)

        return opps

    def _find_cross_platform_arbitrage(
        self, markets: list[Market]
    ) -> list[ArbOpportunity]:
        """
        Trova arbitraggi cross-platform via ArbBets API.

        ArbBets confronta prezzi tra Polymarket, Kalshi e Opinion.
        Noi filtriamo solo quelli che coinvolgono Polymarket e
        cerchiamo un match con i nostri mercati per poter eseguire.
        """
        if not self.arbbets or not self.arbbets.available:
            return []

        poly_arbs = self.arbbets.get_polymarket_arbs()
        if not poly_arbs:
            return []

        opps: list[ArbOpportunity] = []

        # Build lookup veloce per mercati Polymarket
        market_by_slug: dict[str, Market] = {}
        market_by_question_words: dict[str, Market] = {}
        for m in markets:
            if m.slug:
                market_by_slug[m.slug.lower()] = m
            # Indice per parole chiave della domanda (per matching fuzzy)
            words = set(m.question.lower().split())
            key = " ".join(sorted(words)[:5])  # Prime 5 parole ordinate
            market_by_question_words[key] = m

        for arb in poly_arbs:
            if arb.roi < self.min_edge:
                continue

            # Prova a matchare con un mercato Polymarket locale
            matched_market = None

            # 1. Match per slug
            if arb.polymarket_slug:
                matched_market = market_by_slug.get(arb.polymarket_slug.lower())

            # 2. Match per token ID
            if not matched_market and arb.polymarket_token_id:
                for m in markets:
                    if (arb.polymarket_token_id in m.tokens.get("yes", "") or
                            arb.polymarket_token_id in m.tokens.get("no", "")):
                        matched_market = m
                        break

            # 3. Match fuzzy per nome mercato
            if not matched_market and arb.market_name:
                arb_words = set(arb.market_name.lower().split())
                best_match = None
                best_overlap = 0
                for m in markets:
                    m_words = set(m.question.lower().split())
                    overlap = len(arb_words & m_words)
                    if overlap > best_overlap and overlap >= 3:
                        best_overlap = overlap
                        best_match = m
                if best_match:
                    matched_market = best_match

            # Anche senza match locale, segnaliamo l'arb (l'utente puo' eseguire manualmente)
            target_markets = [matched_market] if matched_market else []

            # Determina l'azione
            poly_side = "a" if "polymarket" in arb.platform_a else "b"
            other_platform = arb.platform_b if poly_side == "a" else arb.platform_a
            poly_price = arb.price_a if poly_side == "a" else arb.price_b
            other_price = arb.price_b if poly_side == "a" else arb.price_a

            # Il lato con prezzo piu' basso e' dove compriamo YES
            if poly_price < other_price:
                action_str = (
                    f"BUY YES on Polymarket @{poly_price:.3f} + "
                    f"BUY NO on {other_platform.title()} @{1 - other_price:.3f} | "
                    f"ROI={arb.roi:.2%} | '{arb.market_name[:60]}'"
                )
            else:
                action_str = (
                    f"BUY YES on {other_platform.title()} @{other_price:.3f} + "
                    f"BUY NO on Polymarket @{1 - poly_price:.3f} | "
                    f"ROI={arb.roi:.2%} | '{arb.market_name[:60]}'"
                )

            opp = ArbOpportunity(
                type="cross_platform",
                markets=target_markets,
                edge=arb.roi,
                action=action_str,
                confidence=0.90,  # Cross-platform arb e' quasi risk-free
                cross_platform_arb=arb,
            )
            opps.append(opp)

        if opps:
            logger.info(
                f"[ARB] ArbBets: {len(poly_arbs)} arb Polymarket, "
                f"{len(opps)} con edge > {self.min_edge:.2%}"
            )

        return opps

    def _find_dome_arbitrage(
        self, markets: list[Market]
    ) -> list[ArbOpportunity]:
        """
        Trova arbitraggi cross-platform via Dome API.

        Dome aggrega dati da Polymarket, Kalshi e PredictIt.
        Funziona come backup/complemento di ArbBets con dati piu' strutturati.
        """
        if not self.dome or not self.dome.available:
            return []

        dome_arbs = self.dome.get_polymarket_arbs()
        if not dome_arbs:
            return []

        opps: list[ArbOpportunity] = []

        # Lookup veloce per matching mercati locali
        market_by_slug: dict[str, Market] = {}
        for m in markets:
            if m.slug:
                market_by_slug[m.slug.lower()] = m

        for darb in dome_arbs:
            if darb.roi < self.min_edge:
                continue

            # Prova a matchare con mercato Polymarket locale
            matched_market = None

            # 1. Match per slug
            if darb.polymarket_slug:
                matched_market = market_by_slug.get(darb.polymarket_slug.lower())

            # 2. Match per token ID
            if not matched_market and darb.polymarket_token_id:
                for m in markets:
                    if (darb.polymarket_token_id in m.tokens.get("yes", "") or
                            darb.polymarket_token_id in m.tokens.get("no", "")):
                        matched_market = m
                        break

            # 3. Match fuzzy
            if not matched_market and darb.market_name:
                arb_words = set(darb.market_name.lower().split())
                best_match = None
                best_overlap = 0
                for m in markets:
                    m_words = set(m.question.lower().split())
                    overlap = len(arb_words & m_words)
                    if overlap > best_overlap and overlap >= 3:
                        best_overlap = overlap
                        best_match = m
                if best_match:
                    matched_market = best_match

            target_markets = [matched_market] if matched_market else []

            # Azione
            poly_side = "a" if "polymarket" in darb.platform_a else "b"
            other_platform = darb.platform_b if poly_side == "a" else darb.platform_a
            poly_price = darb.price_a if poly_side == "a" else darb.price_b
            other_price = darb.price_b if poly_side == "a" else darb.price_a

            if poly_price < other_price:
                action_str = (
                    f"BUY YES on Polymarket @{poly_price:.3f} + "
                    f"BUY NO on {other_platform.title()} @{1 - other_price:.3f} | "
                    f"ROI={darb.roi:.2%} [DOME] | '{darb.market_name[:60]}'"
                )
            else:
                action_str = (
                    f"BUY YES on {other_platform.title()} @{other_price:.3f} + "
                    f"BUY NO on Polymarket @{1 - poly_price:.3f} | "
                    f"ROI={darb.roi:.2%} [DOME] | '{darb.market_name[:60]}'"
                )

            opp = ArbOpportunity(
                type="cross_platform_dome",
                markets=target_markets,
                edge=darb.roi,
                action=action_str,
                confidence=0.90,
                cross_platform_arb=CrossPlatformArb(
                    arb_id=darb.arb_id,
                    market_name=darb.market_name,
                    platform_a=darb.platform_a,
                    platform_b=darb.platform_b,
                    price_a=darb.price_a,
                    price_b=darb.price_b,
                    roi=darb.roi,
                    total_cost=darb.total_cost,
                    category=darb.category,
                    polymarket_slug=darb.polymarket_slug,
                    polymarket_token_id=darb.polymarket_token_id,
                    updated_at=darb.updated_at,
                ),
            )
            opps.append(opp)

        if opps:
            logger.info(
                f"[ARB] Dome: {len(dome_arbs)} arb Polymarket, "
                f"{len(opps)} con edge > {self.min_edge:.2%}"
            )

        return opps

    def _extract_price_threshold_markets(
        self, markets: list[Market], asset: str
    ) -> list[tuple[float, Market]]:
        """Estrai mercati con soglie di prezzo numeriche per un asset."""
        results = []
        patterns = [
            rf"{asset}.*(?:above|over|sopra|>\s*)\$?([\d,]+)",
            rf"{asset}.*(?:below|under|sotto|<\s*)\$?([\d,]+)",
            rf"\$?([\d,]+).*{asset}",
        ]

        for m in markets:
            q = m.question.lower()
            if asset not in q:
                continue

            for pattern in patterns:
                match = re.search(pattern, q)
                if match:
                    try:
                        threshold = float(match.group(1).replace(",", ""))
                        results.append((threshold, m))
                        break
                    except ValueError:
                        continue

        return results

    async def execute(self, opportunity: ArbOpportunity, paper: bool = True) -> bool:
        """Esegue un'opportunita' di arbitraggio."""
        if not opportunity.is_profitable:
            return False

        market = opportunity.markets[0]
        size = self.risk.kelly_size(
            win_prob=opportunity.confidence,
            price=0.5,
            strategy=STRATEGY_NAME,
            is_maker=False,  # Arb usa buy_market (taker) per esecuzione immediata
        )

        if size == 0:
            return False

        allowed, reason = self.risk.can_trade(STRATEGY_NAME, size, side="BUY_YES", market_id=market.id)
        if not allowed:
            logger.info(f"Trade bloccato: {reason}")
            return False

        if opportunity.type == "simple":
            return await self._execute_simple(opportunity, size, paper)
        elif opportunity.type == "cross_market":
            return await self._execute_cross(opportunity, size, paper)
        elif opportunity.type in ("cross_platform", "cross_platform_dome"):
            return await self._execute_cross_platform(opportunity, size, paper)

        return False

    async def _execute_simple(
        self, opp: ArbOpportunity, size: float, paper: bool
    ) -> bool:
        """Esegui arbitraggio semplice (compra YES + NO)."""
        m = opp.markets[0]
        half = size / 2

        trade_yes = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id=m.id,
            token_id=m.tokens["yes"],
            side="BUY_YES",
            size=half,
            price=m.prices["yes"],
            edge=opp.edge,
            reason=f"Simple arb: {opp.action}",
        )

        if paper:
            logger.info(f"[PAPER] ARB SIMPLE: {opp.action} size=${size:.2f}")
            self.risk.open_trade(trade_yes)
            # Simulazione: arbitraggio semplice vince quasi sempre
            self.risk.close_trade(m.tokens["yes"], won=True, pnl=size * opp.edge)
        else:
            r1 = self.api.buy_market(m.tokens["yes"], half)
            r2 = self.api.buy_market(m.tokens["no"], half)
            if r1 and r2:
                self.risk.open_trade(trade_yes)

        self._trades_executed += 1
        return True

    async def _execute_cross(
        self, opp: ArbOpportunity, size: float, paper: bool
    ) -> bool:
        """Esegui arbitraggio cross-market."""
        if len(opp.markets) < 2:
            return False

        m_low, m_high = opp.markets[0], opp.markets[1]

        trade = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id=f"{m_low.id}|{m_high.id}",
            token_id=m_low.tokens["yes"],
            side="BUY_YES",
            size=size,
            price=m_low.prices["yes"],
            edge=opp.edge,
            reason=f"Cross arb: {opp.action}",
        )

        if paper:
            logger.info(f"[PAPER] ARB CROSS: {opp.action} size=${size:.2f}")
            self.risk.open_trade(trade)
            won = opp.confidence > 0.5
            pnl = size * opp.edge if won else -size * 0.3
            self.risk.close_trade(m_low.tokens["yes"], won=won, pnl=pnl)
        else:
            r = self.api.buy_market(m_low.tokens["yes"], size)
            if r:
                self.risk.open_trade(trade)

        self._trades_executed += 1
        return True

    async def _execute_cross_platform(
        self, opp: ArbOpportunity, size: float, paper: bool
    ) -> bool:
        """
        Esegui arbitraggio cross-platform (Polymarket vs Kalshi/Opinion).

        In paper trading: simula solo il lato Polymarket.
        In live: esegue il lato Polymarket (il lato Kalshi/Opinion
        va eseguito manualmente o con un client Kalshi separato).
        """
        arb = opp.cross_platform_arb
        if not arb:
            return False

        # Se abbiamo un mercato Polymarket matchato, eseguiamo il nostro lato
        if opp.markets:
            m = opp.markets[0]
            # Determina se compriamo YES o NO su Polymarket
            poly_side = "a" if "polymarket" in arb.platform_a else "b"
            poly_price = arb.price_a if poly_side == "a" else arb.price_b
            other_price = arb.price_b if poly_side == "a" else arb.price_a

            # Se Polymarket e' piu' economico → compra YES su Poly
            buy_yes_on_poly = poly_price < other_price
            token_key = "yes" if buy_yes_on_poly else "no"
            token_id = m.tokens.get(token_key, "")
            price = m.prices.get(token_key, 0.5)

            trade = Trade(
                timestamp=time.time(),
                strategy=STRATEGY_NAME,
                market_id=m.id,
                token_id=token_id,
                side=f"BUY_{token_key.upper()}",
                size=size,
                price=price,
                edge=opp.edge,
                reason=f"XPlat arb via ArbBets: {opp.action}",
            )

            if paper:
                logger.info(
                    f"[PAPER] ARB XPLAT: {opp.action} size=${size:.2f} "
                    f"(⚠ solo lato Polymarket — lato "
                    f"{arb.platform_b if poly_side == 'a' else arb.platform_a} "
                    f"da eseguire manualmente)"
                )
                self.risk.open_trade(trade)
                # Cross-platform arb ha win rate molto alto
                import random
                won = random.random() < 0.85
                pnl = size * opp.edge if won else -size * 0.2
                self.risk.close_trade(token_id, won=won, pnl=pnl)
            else:
                result = self.api.buy_market(token_id, size)
                if result:
                    self.risk.open_trade(trade)
                    logger.warning(
                        f"[LIVE] ARB XPLAT: Lato Polymarket eseguito. "
                        f"ESEGUI MANUALMENTE il lato "
                        f"{arb.platform_b if poly_side == 'a' else arb.platform_a}!"
                    )
        else:
            # Nessun match locale — logga solo come segnale
            logger.info(
                f"[SIGNAL] ARB XPLAT: {opp.action} (no match locale — "
                f"esegui manualmente su entrambe le piattaforme)"
            )
            return False

        self._trades_executed += 1
        return True

    @property
    def stats(self) -> dict:
        return {
            "opportunities_found": self._opportunities_found,
            "trades_executed": self._trades_executed,
            "cross_platform_found": self._cross_platform_found,
        }
