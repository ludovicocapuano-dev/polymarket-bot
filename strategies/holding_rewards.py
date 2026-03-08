"""
Holding Rewards Strategy v1.0

Polymarket pays 4% APY on positions in 13 eligible long-term markets.
Reward = position_value * (4% / 365) per day, sampled hourly.

Strategy: identify eligible markets, buy the favorite (highest implied prob)
with a small position to earn passive yield. Only in fee-free markets.

Eligible markets (as of Mar 2026): 2028 US presidential, 2026 midterms,
Russia/China/Turkey/Israel/Ukraine leadership.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Keywords to match eligible holding-reward markets
ELIGIBLE_KEYWORDS = [
    # 2028 US Presidential
    "2028 presidential", "president in 2028", "win 2028",
    "2028 election", "president 2029",
    # 2026 Midterms
    "2026 midterm", "senate 2026", "house 2026",
    "midterm election",
    # Leadership markets
    "president of russia", "leader of russia", "putin",
    "president of china", "xi jinping", "leader of china",
    "president of turkey", "erdogan", "leader of turkey",
    "prime minister of israel", "netanyahu", "leader of israel",
    "president of ukraine", "zelensky", "leader of ukraine",
]

# Fee-free categories where holding rewards apply
FEE_FREE_CATEGORIES = {"politics", "geopolitics", "elections", ""}


@dataclass
class HoldingRewardOpportunity:
    market_id: str
    question: str
    side: str  # "YES" or "NO"
    price: float
    implied_prob: float
    daily_yield_per_100: float  # $ yield per day per $100 position
    token_id: str


class HoldingRewardsStrategy:
    """Buys positions in eligible long-term markets for 4% APY holding rewards."""

    APY = 0.04
    MIN_POSITION = 20.0  # v11.1: ridotto — yield irrisorio, rischio reale
    MAX_POSITION = 50.0
    SCAN_INTERVAL = 3600  # scan every hour (positions are long-term)
    MIN_VOLUME = 50000  # v11.1: alzato da 5K — mercati liquidi hanno prezzi migliori
    # v11.1: solo favoriti quasi-certi. @$0.78 il rischio non giustifica 4% APY.
    # A $0.92 serve 95.7% WR per break-even sul solo stake. Con 4% APY
    # il break-even scende a ~93%. Solo prezzi >0.90 hanno senso.
    MIN_FAVORITE_PRICE = 0.90
    MAX_FAVORITE_PRICE = 0.97

    def __init__(self):
        self._last_scan = 0.0
        self._held_markets: set[str] = set()  # condition_ids already positioned
        self._total_yield = 0.0

    def _is_eligible(self, market) -> bool:
        q = market.question.lower()
        return any(kw in q for kw in ELIGIBLE_KEYWORDS)

    def scan(self, markets: list, existing_positions: set[str] = None) -> list[HoldingRewardOpportunity]:
        now = time.time()
        if now - self._last_scan < self.SCAN_INTERVAL:
            return []
        self._last_scan = now

        if existing_positions:
            self._held_markets = existing_positions

        opportunities = []
        eligible_count = 0

        for m in markets:
            if not m.active:
                continue
            if not self._is_eligible(m):
                continue

            eligible_count += 1

            # Skip if already positioned
            if m.condition_id in self._held_markets:
                continue

            # Skip low-volume markets
            if m.volume < self.MIN_VOLUME:
                continue

            # Determine favorite side
            p_yes = m.prices.get("yes", 0.5)
            p_no = m.prices.get("no", 0.5)

            if p_yes >= p_no:
                side, price, token_id = "YES", p_yes, m.tokens.get("yes", "")
            else:
                side, price, token_id = "NO", p_no, m.tokens.get("no", "")

            # Only buy favorites in safe range
            if price < self.MIN_FAVORITE_PRICE or price > self.MAX_FAVORITE_PRICE:
                continue

            if not token_id:
                continue

            daily_yield = self.APY / 365 * 100  # per $100 position
            opp = HoldingRewardOpportunity(
                market_id=m.condition_id,
                question=m.question[:80],
                side=side,
                price=price,
                implied_prob=price,
                daily_yield_per_100=round(daily_yield, 4),
                token_id=token_id,
            )
            opportunities.append(opp)

        if eligible_count > 0:
            logger.info(
                f"[HOLD-REWARDS] {eligible_count} eligible markets, "
                f"{len(self._held_markets)} already held, "
                f"{len(opportunities)} new opportunities"
            )

        return opportunities

    def execute(self, opp: HoldingRewardOpportunity, api, risk, live: bool = False) -> bool:
        size = self.MIN_POSITION

        if not live:
            self._held_markets.add(opp.market_id)
            logger.info(
                f"[HOLD-REWARDS] PAPER BUY {opp.side} ${size:.0f} @{opp.price:.2f} "
                f"'{opp.question[:50]}' yield=${opp.daily_yield_per_100:.3f}/day/$100"
            )
            return True

        try:
            result = api.smart_buy(
                token_id=opp.token_id,
                amount=size,
                target_price=opp.price,
            )
            if result:
                self._held_markets.add(opp.market_id)
                logger.info(
                    f"[HOLD-REWARDS] BUY {opp.side} ${size:.0f} @{opp.price:.2f} "
                    f"'{opp.question[:50]}' yield=${opp.daily_yield_per_100:.3f}/day/$100"
                )
                return True
            else:
                logger.warning(f"[HOLD-REWARDS] Order failed: {opp.question[:50]}")
                return False
        except Exception as e:
            logger.warning(f"[HOLD-REWARDS] Error: {e}")
            return False

    @property
    def stats(self) -> dict:
        return {
            "held_markets": len(self._held_markets),
            "est_daily_yield": round(len(self._held_markets) * self.MIN_POSITION * self.APY / 365, 4),
        }
