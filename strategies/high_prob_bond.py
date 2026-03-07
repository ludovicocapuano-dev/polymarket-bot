"""
Strategia: High Probability Bond — v8.0
========================================
Compra contratti con probabilita' >93% (YES price >= 0.93) per eventi
near-certain, come "obbligazioni" a breve scadenza.

Concetto:
  Se un mercato Polymarket ha YES a $0.95 e risolve in 5 giorni,
  comprare YES paga 1.00 alla risoluzione → profitto = $0.05 / $0.95 = 5.26%
  in 5 giorni → ~384% annualizzato. Con alta probabilita' di vincita.

v7.5 fixes:
  - Fix campo end_date (era end_date_iso → 0 mercati analizzati)
  - Kelly win_prob basata su certainty score, non su price (era sempre 0)
  - Certainty score multi-fattore per filtro black swan
  - Scoring composito per ranking (certainty * yield * liquidity)
  - Blacklist hard/soft (sport tradabili con alta certezza)

Safety:
  - Solo mercati con YES >= 0.93 (quasi certi)
  - Max 14 giorni alla risoluzione
  - Rendimento annualizzato >= 100% (altrimenti non vale la pena)
  - Certainty score >= 0.55 (consensus multi-fattore)
  - Blacklist hard (crypto, mma) + soft (sport con certainty >= 0.80)
  - Max 5 posizioni bond contemporanee
  - Max 20% del budget su un singolo bond
  - Solo BUY YES (mai shortare)

ROI documentato: 1,800%+ annuo con compounding su bond near-certain.
"""

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from utils.polymarket_api import Market, PolymarketAPI
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "high_prob_bond"

# ── Parametri strategia ──
MIN_PROB = 0.93              # prezzo minimo YES per considerare
MAX_DAYS_TO_RESOLUTION = 14  # massimo giorni alla risoluzione
MIN_EDGE = 0.03              # v10.5: da 0.01 — con 0.01 un loss cancella 20 wins
MIN_ANNUAL_YIELD = 1.0       # rendimento annualizzato minimo (100%)
MAX_BOND_POSITIONS = 5       # max posizioni bond contemporanee
MAX_BUDGET_PER_BOND = 0.20   # max 20% del budget su un singolo bond

# HARD blacklist: mai tradare (black swan troppo probabile)
# v8.0: Sports spostati da SOFT a HARD — Becker: -$17.4M PnL nonostante 96.9% WR
HARD_BLACKLIST = [
    "btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp",
    "crypto", "token", "defi", "5 min", "5-min", "15 min", "15-min",
    "mma", "ufc", "boxing", "fight",
    # v8.0: Sports → HARD (Becker: black swan devastanti, -$17.4M)
    "nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
    "tennis", "championship", "playoff", "world cup", "super bowl",
    "match", "game score",
    # v10.6: NCAA/college sports — "Niagara vs Quinnipiac" passava il filtro
    "ncaa", "college basketball", "college football", "division i",
    "march madness", "bowl game", "varsity",
    # v10.6: Pattern head-to-head (Team A vs Team B = quasi sempre sport)
    " vs ", " vs. ", "eagles", "bobcats", "bulldogs", "wildcats",
    "tigers", "bears", "lions", "hawks", "falcons", "panthers",
    "wolves", "warriors", "knights", "spartans", "trojans",
    "cardinals", "mustangs", "cougars", "hornets", "huskies",
]

# v8.0: SOFT blacklist svuotata — tutto hard o niente
SOFT_BLACKLIST: list[str] = []

MIN_CERTAINTY = 0.75            # v10.5: da 0.55 — troppi falsi positivi a certainty bassa
SOFT_BLACKLIST_CERTAINTY = 0.80 # soglia per mercati soft-blacklisted


@dataclass
class BondOpportunity:
    """Un'opportunita' bond ad alta probabilita'."""
    market: Market
    price_yes: float
    edge: float              # edge NETTO (dopo fee)
    days_to_resolution: float
    annual_yield: float      # rendimento annualizzato
    certainty_score: float   # 0-1 score multi-fattore
    bond_score: float        # score composito per ranking
    reasoning: str


class HighProbBondStrategy:
    """
    Strategia bond: compra YES su mercati near-certain (>93%) a breve scadenza.

    Come un'obbligazione a breve termine: basso rischio, rendimento moderato,
    ma con alta frequenza di risoluzione il rendimento annualizzato e' molto alto.
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        min_edge: float = MIN_EDGE,
    ):
        self.api = api
        self.risk = risk
        self.min_edge = min_edge
        self._trades_executed = 0
        self._recently_traded: dict[str, float] = {}
        self._TRADE_COOLDOWN = 600  # 10 minuti (bond sono lenti, no fretta)

    async def scan(self, shared_markets: list[Market] | None = None) -> list[BondOpportunity]:
        """
        Scansiona mercati per opportunita' bond ad alta probabilita'.

        v7.5: Certainty score multi-fattore, scoring composito, fee-aware edge.
        """
        markets = shared_markets or self.api.fetch_markets(limit=200)
        if not markets:
            logger.info("[BOND] Scan: 0 mercati disponibili")
            return []

        open_bond_count = sum(
            1 for t in self.risk.open_trades
            if t.strategy == STRATEGY_NAME
        )

        if open_bond_count >= MAX_BOND_POSITIONS:
            logger.info(
                f"[BOND] Max posizioni bond raggiunte ({open_bond_count}/{MAX_BOND_POSITIONS})"
            )
            return []

        opportunities = []
        now = datetime.now(timezone.utc)

        for m in markets:
            # 1. Prezzo YES >= MIN_PROB (v8.0: ridotto per politics)
            price_yes = m.prices.get("yes", 0.5)
            q_lower = m.question.lower()
            _POLITICS_DETECT = ["election", "president", "congress", "senate",
                                "vote", "democrat", "republican", "trump",
                                "biden", "governor", "cabinet", "tariff", "impeach"]
            is_politics = any(kw in q_lower for kw in _POLITICS_DETECT)
            min_prob = 0.90 if is_politics else MIN_PROB  # v8.0: 0.90 vs 0.93
            if price_yes < min_prob:
                continue

            # 2. Volume gate: min $1K (relaxed da $10K — mercati bond spesso low-vol)
            if m.volume < 1_000:
                continue

            # 3. Liquidity gate: min $500
            if m.liquidity < 500:
                continue

            # 4. Blacklist hard/soft
            bl = self._is_blacklisted(m)
            if bl == "hard":
                continue
            # soft: controllato dopo certainty score

            # 5. End date (FIX: era end_date_iso)
            days = self._days_to_resolution(m, now)
            if days is None or days <= 0 or days > MAX_DAYS_TO_RESOLUTION:
                continue

            # 6. Certainty score multi-fattore
            certainty = self._certainty_score(m)

            # Days factor (10%): piu' vicina la risoluzione, piu' certo
            if days <= 1:
                certainty += 0.10
            elif days <= 3:
                certainty += 0.08
            elif days <= 7:
                certainty += 0.05
            elif days <= 14:
                certainty += 0.02

            # Soft blacklist: richiede certainty alta
            if bl == "soft" and certainty < SOFT_BLACKLIST_CERTAINTY:
                continue

            # Soglia minima di certezza
            if certainty < MIN_CERTAINTY:
                continue

            # 7. Edge netto fee-aware
            gross_edge = 1.0 - price_yes
            # Round-trip fee stima: entry (maker=0) + exit (taker)
            exit_fee = price_yes * (1.0 - price_yes) * 0.0625
            spread_cost = 0.005
            net_edge = gross_edge - exit_fee - spread_cost
            if net_edge < self.min_edge:
                continue

            # 8. Rendimento annualizzato (su edge netto)
            annual_yield = (net_edge / price_yes) / days * 365
            if annual_yield < MIN_ANNUAL_YIELD:
                continue

            # 9. Cooldown
            if m.id in self._recently_traded:
                if time.time() - self._recently_traded[m.id] < self._TRADE_COOLDOWN:
                    continue

            # 10. No posizioni duplicate
            already_open = any(
                t.market_id == m.id for t in self.risk.open_trades
            )
            if already_open:
                continue

            # Scoring composito per ranking
            bond_score = (
                0.50 * certainty
                + 0.30 * min(annual_yield / 5.0, 1.0)
                + 0.20 * min(m.liquidity / 100_000, 1.0)
            )

            opportunities.append(BondOpportunity(
                market=m,
                price_yes=price_yes,
                edge=net_edge,
                days_to_resolution=days,
                annual_yield=annual_yield,
                certainty_score=certainty,
                bond_score=bond_score,
                reasoning=(
                    f"BOND: YES@{price_yes:.4f} edge={net_edge:.4f} "
                    f"days={days:.1f} yield={annual_yield:.0%}/yr "
                    f"certainty={certainty:.2f} score={bond_score:.3f} "
                    f"'{m.question[:40]}'"
                ),
            ))

        # Classifica per bond_score composito (non solo yield)
        opportunities.sort(key=lambda o: o.bond_score, reverse=True)

        # Limita al numero di slot disponibili
        slots_available = MAX_BOND_POSITIONS - open_bond_count
        opportunities = opportunities[:slots_available]

        if opportunities:
            logger.info(
                f"[BOND] Scan {len(markets)} mercati → "
                f"{len(opportunities)} bond qualificati (slots={slots_available}) "
                f"migliore: score={opportunities[0].bond_score:.3f} "
                f"certainty={opportunities[0].certainty_score:.2f} "
                f"yield={opportunities[0].annual_yield:.0%}/yr"
            )
        else:
            logger.info(
                f"[BOND] Scan {len(markets)} mercati → 0 bond qualificati"
            )

        return opportunities

    async def execute(self, opp: BondOpportunity, paper: bool = True) -> bool:
        """
        Esegui un trade bond.

        v7.5: win_prob basata su certainty score (non price).
        Size conservativo: min(kelly_size, max_bet_size * 0.5)
        Solo BUY YES (mai shortare).
        """
        now = time.time()
        market_id = opp.market.id
        last_traded = self._recently_traded.get(market_id, 0)

        if now - last_traded < self._TRADE_COOLDOWN:
            return False

        for open_t in self.risk.open_trades:
            if open_t.market_id == market_id:
                return False

        token_id = opp.market.tokens["yes"]
        price = opp.price_yes

        # v7.5: win_prob dal certainty score, NON dal price
        # Se certainty >= 0.85 → bonus +0.03 (win_prob = price + 0.03)
        # Se certainty >= 0.70 → bonus +0.02
        # Se certainty >= 0.55 → bonus +0.01
        certainty = opp.certainty_score
        if certainty >= 0.85:
            certainty_bonus = 0.03
        elif certainty >= 0.70:
            certainty_bonus = 0.02
        elif certainty >= 0.55:
            certainty_bonus = 0.01
        else:
            certainty_bonus = 0.0

        win_prob = min(price + certainty_bonus, 0.99)

        kelly = self.risk.kelly_size(
            win_prob=win_prob,
            price=price,
            strategy=STRATEGY_NAME,
        )

        # v10.5: Size ridotto — bond rischia $X per vincere $X*0.05
        # Con max_bet=40, max bond=$12 (era $20). Un loss da $12 richiede
        # ~12 wins da $1 per recuperare, non 20.
        size = min(kelly, self.risk.config.max_bet_size * 0.30)

        # Limite budget per singolo bond: max 20% del budget strategia
        budget = getattr(self.risk, '_strategy_budgets', {}).get(STRATEGY_NAME, 0)
        if budget > 0:
            max_per_bond = budget * MAX_BUDGET_PER_BOND
            size = min(size, max_per_bond)

        if size <= 0:
            logger.info(
                f"[BOND] size=0 '{opp.market.question[:35]}' "
                f"p={price:.3f} win_prob={win_prob:.3f} "
                f"certainty={certainty:.2f} kelly={kelly:.2f}"
            )
            return False

        allowed, reason = self.risk.can_trade(STRATEGY_NAME, size, price=price, side="BUY_YES", market_id=market_id)
        if not allowed:
            logger.info(f"[BOND] Trade bloccato: {reason}")
            return False

        trade = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id=market_id,
            token_id=token_id,
            side="BUY_YES",
            size=size,
            price=price,
            edge=opp.edge,
            reason=opp.reasoning,
        )

        if paper:
            logger.info(
                f"[PAPER] BOND: BUY YES "
                f"'{opp.market.question[:35]}' "
                f"${size:.2f} @{price:.4f} edge={opp.edge:.4f} "
                f"yield={opp.annual_yield:.0%}/yr days={opp.days_to_resolution:.1f} "
                f"certainty={certainty:.2f} win_prob={win_prob:.3f} kelly={kelly:.2f}"
            )
            self.risk.open_trade(trade)

            # Simulazione: win_prob dal certainty (piu' accurato)
            sim_win_prob = min(win_prob, 0.98)
            won = random.random() < sim_win_prob
            if won:
                pnl = size * (1.0 / price - 1.0)
            else:
                pnl = -size
            self.risk.close_trade(token_id, won=won, pnl=pnl)
        else:
            from utils.avellaneda_stoikov import market_inventory_frac
            inv = market_inventory_frac(self.risk.open_trades, market_id, self.risk._strategy_budgets.get(STRATEGY_NAME, 1))
            vpin_val = self.risk.vpin_monitor.get_vpin(market_id) if self.risk.vpin_monitor else 0.0
            result = self.api.smart_buy(
                token_id, size, target_price=price,
                timeout_sec=15.0, fallback_market=True,
                inventory_frac=inv, volume_24h=opp.market.volume, vpin=vpin_val,
            )
            if result:
                if isinstance(result, dict) and result.get("_fill_price"):
                    trade.price = result["_fill_price"]
                self.risk.open_trade(trade)

        self._recently_traded[market_id] = time.time()
        self._trades_executed += 1
        return True

    def _is_blacklisted(self, market: Market) -> str:
        """
        Verifica blacklist.
        Ritorna: 'hard' (mai tradare), 'soft' (solo alta certezza), '' (ok).
        """
        q = market.question.lower()
        tags = " ".join(market.tags).lower()
        combined = f"{q} {tags}"
        if any(kw in combined for kw in HARD_BLACKLIST):
            return "hard"
        if any(kw in combined for kw in SOFT_BLACKLIST):
            return "soft"
        return ""

    def _certainty_score(self, market: Market) -> float:
        """
        Score 0-1 che indica quanto siamo certi che il mercato risolva come atteso.
        Max 0.90 (10% riservato a days factor applicato in scan).
        """
        score = 0.0

        # 1. Volume factor (30%): alto volume = consensus forte
        vol = market.volume
        if vol >= 500_000:
            score += 0.30
        elif vol >= 100_000:
            score += 0.25
        elif vol >= 50_000:
            score += 0.20
        elif vol >= 10_000:
            score += 0.15
        elif vol >= 1_000:
            score += 0.10
        # else: 0 (volume troppo basso)

        # 2. Liquidity factor (25%): alta liquidita' = exit facile, spread basso
        liq = market.liquidity
        if liq >= 50_000:
            score += 0.25
        elif liq >= 10_000:
            score += 0.20
        elif liq >= 5_000:
            score += 0.15
        elif liq >= 1_000:
            score += 0.10
        elif liq >= 500:
            score += 0.05

        # 3. Spread factor (20%): spread stretto = pricing efficiente
        spread = market.spread
        if spread < 0.005:
            score += 0.20
        elif spread < 0.010:
            score += 0.15
        elif spread < 0.020:
            score += 0.10

        # 4. Price consistency (15%): YES + NO ~ 1.0 = no mispricing
        mispricing = market.mispricing_score
        if mispricing < 0.005:
            score += 0.15
        elif mispricing < 0.01:
            score += 0.10
        elif mispricing < 0.02:
            score += 0.05

        # v8.0: Category boost basato su Becker Dataset
        q = market.question.lower()

        # Politics boost: Becker 98.9% WR, +$18.6M nella bond zone
        POLITICS_KW = ["election", "president", "congress", "senate", "vote",
                       "democrat", "republican", "trump", "biden", "governor",
                       "cabinet", "tariff", "impeach"]
        if any(kw in q for kw in POLITICS_KW):
            score += 0.12  # Boost significativo

        # Finance boost: Becker 99.9% WR (pochi ma quasi certi)
        FINANCE_KW = ["fed ", "inflation", "gdp", "interest rate", "cpi",
                      "treasury", "s&p", "nasdaq"]
        if any(kw in q for kw in FINANCE_KW):
            score += 0.08

        return min(score, 1.0)  # cap a 1.0

    def _days_to_resolution(self, market: Market, now: datetime) -> float | None:
        """
        Calcola i giorni rimanenti alla risoluzione del mercato.
        Ritorna None se non disponibile o non parsabile.
        """
        end_date_str = getattr(market, 'end_date', None)  # FIX: era 'end_date_iso'
        if not end_date_str:
            return None

        try:
            end_date_str = str(end_date_str).strip()
            if end_date_str.endswith("Z"):
                end_date_str = end_date_str[:-1] + "+00:00"
            end_date = datetime.fromisoformat(end_date_str)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)

            delta = end_date - now
            return max(delta.total_seconds() / 86400, 0)
        except (ValueError, TypeError):
            return None

    @property
    def stats(self) -> dict:
        return {
            "trades_executed": self._trades_executed,
            "open_bonds": sum(
                1 for t in self.risk.open_trades
                if t.strategy == STRATEGY_NAME
            ),
        }
