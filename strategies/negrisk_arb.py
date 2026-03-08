"""
NegRisk Sum Arbitrage Scanner v1.0

Monitors multi-outcome (negRisk) markets for sum deviation arbitrage.
When SUM(YES prices) deviates from $1.00 beyond threshold, executes risk-free arb.

Based on research: $29M extracted from Polymarket via this mechanism (2024-2025).
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional
from utils.risk_manager import Trade

logger = logging.getLogger(__name__)


@dataclass
class NegRiskArbOpportunity:
    event_id: str
    event_slug: str
    question: str
    arb_type: str  # 'buy_all' or 'sell_all'
    sum_prices: float
    profit_per_dollar: float
    n_outcomes: int
    outcomes: list  # list of {token_id, price, title}
    total_cost: float  # total cost to execute


class NegRiskArbScanner:
    """Scans negRisk markets for sum deviation arbitrage."""

    # Minimum profit to bother (after spread costs)
    MIN_PROFIT_PCT = 0.02  # 2% minimum profit
    # Maximum we'll deploy per arb
    MAX_ARB_SIZE = 150.0  # v10.8.4: da $100, risk-free
    # Cooldown between arb attempts on same event
    ARB_COOLDOWN = 1800  # 30 minutes

    def __init__(self):
        self._last_arb: dict[str, float] = {}  # event_id -> timestamp
        self._total_profit = 0.0
        self._total_arbs = 0

    def scan(self, markets: list) -> list[NegRiskArbOpportunity]:
        """
        Scan all markets for negRisk sum arbitrage.

        Groups markets by event, checks if sum of YES prices deviates from $1.00.
        """
        # Group markets by event
        events: dict[str, list] = {}
        for m in markets:
            # Only negRisk markets
            if not getattr(m, 'neg_risk', False):
                continue
            event_id = getattr(m, 'event_id', None) or getattr(m, 'condition_id', '')
            if not event_id:
                continue
            # Use event_slug or group_slug for grouping
            group_key = getattr(m, 'event_slug', None) or getattr(m, 'group_slug', None) or event_id
            if group_key not in events:
                events[group_key] = []
            events[group_key].append(m)

        opportunities = []
        now = time.time()

        for event_key, event_markets in events.items():
            # Need at least 2 outcomes
            if len(event_markets) < 2:
                continue

            # Cooldown check
            if event_key in self._last_arb:
                if now - self._last_arb[event_key] < self.ARB_COOLDOWN:
                    continue

            # Calculate sum of YES prices
            outcomes = []
            sum_yes = 0.0
            valid = True

            for m in event_markets:
                price_yes = m.prices.get("yes", 0)
                if price_yes <= 0 or price_yes >= 1:
                    valid = False
                    break
                token_id = getattr(m, 'token_id', None) or m.prices.get('token_id', '')
                outcomes.append({
                    'token_id': token_id,
                    'price': price_yes,
                    'title': getattr(m, 'question', '')[:60],
                    'market_id': getattr(m, 'condition_id', ''),
                })
                sum_yes += price_yes

            if not valid or not outcomes:
                continue

            # Check for arbitrage
            # Buy-all: sum < 1.0 - threshold
            if sum_yes < 1.0 - self.MIN_PROFIT_PCT:
                profit = 1.0 - sum_yes
                opp = NegRiskArbOpportunity(
                    event_id=event_key,
                    event_slug=event_key,
                    question=event_markets[0].question[:80] if hasattr(event_markets[0], 'question') else event_key,
                    arb_type='buy_all',
                    sum_prices=sum_yes,
                    profit_per_dollar=profit / sum_yes,
                    n_outcomes=len(outcomes),
                    outcomes=outcomes,
                    total_cost=sum_yes,
                )
                opportunities.append(opp)
                logger.info(
                    f"[NEGRISK-ARB] BUY_ALL: {event_key} "
                    f"sum={sum_yes:.4f} profit={profit:.4f} ({profit/sum_yes*100:.1f}%) "
                    f"outcomes={len(outcomes)}"
                )

            # Sell-all: sum > 1.0 + threshold
            elif sum_yes > 1.0 + self.MIN_PROFIT_PCT:
                profit = sum_yes - 1.0
                opp = NegRiskArbOpportunity(
                    event_id=event_key,
                    event_slug=event_key,
                    question=event_markets[0].question[:80] if hasattr(event_markets[0], 'question') else event_key,
                    arb_type='sell_all',
                    sum_prices=sum_yes,
                    profit_per_dollar=profit / 1.0,
                    n_outcomes=len(outcomes),
                    outcomes=outcomes,
                    total_cost=1.0,
                )
                opportunities.append(opp)
                logger.info(
                    f"[NEGRISK-ARB] SELL_ALL: {event_key} "
                    f"sum={sum_yes:.4f} profit={profit:.4f} ({profit*100:.1f}%) "
                    f"outcomes={len(outcomes)}"
                )

        if opportunities:
            logger.info(f"[NEGRISK-ARB] Scan {len(events)} events -> {len(opportunities)} arb opportunities")
        else:
            logger.debug(f"[NEGRISK-ARB] Scan {len(events)} negRisk events -> 0 arb")

        return opportunities

    def execute(self, opp: NegRiskArbOpportunity, api, risk, live: bool = False) -> bool:
        """
        Execute a negRisk arbitrage opportunity.

        For buy_all: buy YES on every outcome.
        For sell_all: sell YES (buy NO) on every outcome.
        """
        max_size = min(self.MAX_ARB_SIZE, opp.total_cost * 2)

        if not live:
            # Paper trade
            profit = opp.profit_per_dollar * max_size
            self._total_profit += profit
            self._total_arbs += 1
            self._last_arb[opp.event_id] = time.time()
            logger.info(
                f"[NEGRISK-ARB] PAPER {opp.arb_type}: {opp.event_slug} "
                f"profit=${profit:.2f} (size=${max_size:.2f})"
            )
            return True

        # Live execution — buy/sell all outcomes
        # This is risk-free so we can be aggressive with sizing
        logger.info(
            f"[NEGRISK-ARB] LIVE {opp.arb_type}: {opp.event_slug} "
            f"sum={opp.sum_prices:.4f} profit_pct={opp.profit_per_dollar*100:.1f}% "
            f"outcomes={opp.n_outcomes}"
        )

        success_count = 0
        for outcome in opp.outcomes:
            try:
                side = "BUY" if opp.arb_type == 'buy_all' else "SELL"
                # Size per leg = total_size / n_outcomes (equal weight)
                leg_size = max_size / opp.n_outcomes

                if side == "BUY":
                    result = api.smart_buy(
                        token_id=outcome['token_id'],
                        amount=leg_size,
                        target_price=outcome['price'],
                    )
                else:
                    result = api.smart_sell(
                        token_id=outcome['token_id'],
                        shares=leg_size,
                        current_price=outcome['price'],
                    )

                if result:
                    success_count += 1
                    logger.info(f"[NEGRISK-ARB] Leg OK: {outcome['title'][:40]} {side} ${leg_size:.2f}")
                else:
                    logger.warning(f"[NEGRISK-ARB] Leg FAIL: {outcome['title'][:40]}")

            except Exception as e:
                logger.warning(f"[NEGRISK-ARB] Leg error: {outcome['title'][:40]}: {e}")

        self._last_arb[opp.event_id] = time.time()

        if success_count == opp.n_outcomes:
            profit = opp.profit_per_dollar * max_size
            self._total_profit += profit
            self._total_arbs += 1
            logger.info(
                f"[NEGRISK-ARB] COMPLETE: {opp.event_slug} "
                f"all {success_count} legs filled, profit~${profit:.2f}"
            )
            # v12.0.1: register arb as single trade in risk manager
            if risk:
                trade = Trade(
                    timestamp=time.time(),
                    strategy="negrisk_arb",
                    market_id=opp.event_id,
                    token_id=opp.outcomes[0]['token_id'] if opp.outcomes else "",
                    side=f"ARB_{opp.arb_type.upper()}",
                    size=max_size,
                    price=opp.sum_prices,
                    edge=opp.profit_per_dollar,
                    reason=f"negrisk {opp.n_outcomes} legs",
                )
                risk.open_trade(trade)
            return True
        else:
            logger.warning(
                f"[NEGRISK-ARB] PARTIAL: {opp.event_slug} "
                f"{success_count}/{opp.n_outcomes} legs filled — EXPOSURE RISK"
            )
            return False

    @property
    def stats(self) -> dict:
        return {
            "total_arbs": self._total_arbs,
            "total_profit": round(self._total_profit, 2),
        }
