"""
Favorite-Longshot Bias Exploitation v1.0

Academic evidence (NBER, Economica): participants systematically overvalue
longshots and undervalue favorites. Edge: 2-6% on favorites priced $0.70-$0.90.

Strategy: systematically buy favorites in fee-free markets where:
1. Price is $0.70-$0.90 (strong favorite but not near-certain)
2. Market is fee-free (politics, geopolitics, entertainment, science)
3. Sufficient volume (bias strongest with retail participation)
4. Not a weather market (already covered by weather strategy)
5. Not a crypto market (fees kill the edge)

Position sizing: quarter-Kelly with conservative 3% estimated edge.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Categories where longshot bias is strongest (fee-free, retail-heavy)
BIAS_CATEGORIES = {"politics", "pop-culture", "entertainment", "science",
                   "sports", "world", "geopolitics", "elections", ""}

# Keywords to EXCLUDE (already covered by other strategies or problematic)
EXCLUDE_KEYWORDS = [
    "temperature", "weather", "highest temp", "lowest temp",  # weather strategy
    "bitcoin price", "btc price", "eth price", "crypto price",  # crypto (fees)
]


@dataclass
class FavoriteLongshotOpportunity:
    market_id: str
    question: str
    side: str
    price: float
    edge: float  # estimated systematic edge
    token_id: str
    volume: float
    category: str


class FavoriteLongshotStrategy:
    """Exploits the favorite-longshot bias in prediction markets."""

    # Price band for favorites
    MIN_PRICE = 0.70
    MAX_PRICE = 0.90
    # Estimated systematic edge (conservative — literature says 2-6%)
    BASE_EDGE = 0.03
    # Volume/liquidity filters
    MIN_VOLUME = 50_000  # need sufficient retail participation
    MIN_LIQUIDITY = 1_000
    # Position limits
    MAX_BET = 40.0  # v10.8.4: da $25, proporzionale al capitale
    MAX_POSITIONS = 10  # diversify across many markets
    # Timing
    SCAN_INTERVAL = 1800  # every 30 min
    COOLDOWN_PER_MARKET = 86400  # 24h cooldown per market

    def __init__(self):
        self._last_scan = 0.0
        self._positions: dict[str, float] = {}  # market_id -> timestamp
        self._total_profit = 0.0
        self._total_trades = 0

    def _is_excluded(self, market) -> bool:
        q = market.question.lower()
        return any(kw in q for kw in EXCLUDE_KEYWORDS)

    def _estimate_edge(self, price: float, volume: float) -> float:
        """Estimate bias edge based on price and market characteristics.

        Bias is strongest for:
        - Prices 0.75-0.85 (moderate favorites)
        - High-volume retail markets (more unsophisticated participants)
        """
        # Base edge from literature
        edge = self.BASE_EDGE

        # Price adjustment: bias peaks around 0.80, weaker at extremes
        if 0.75 <= price <= 0.85:
            edge *= 1.2  # sweet spot
        elif price > 0.88:
            edge *= 0.7  # near-certain, less bias

        # Volume adjustment: more retail = more bias
        if volume > 500_000:
            edge *= 1.15  # high-volume retail market
        elif volume < 100_000:
            edge *= 0.85  # low activity, may be efficient

        return round(edge, 4)

    def scan(self, markets: list) -> list[FavoriteLongshotOpportunity]:
        now = time.time()
        if now - self._last_scan < self.SCAN_INTERVAL:
            return []
        self._last_scan = now

        # Check position limit
        active = sum(1 for ts in self._positions.values()
                     if now - ts < self.COOLDOWN_PER_MARKET)
        if active >= self.MAX_POSITIONS:
            logger.debug(f"[FAV-LONG] Max positions ({self.MAX_POSITIONS}) reached")
            return []

        opportunities = []
        scanned = 0

        for m in markets:
            if not m.active:
                continue
            if self._is_excluded(m):
                continue
            if m.volume < self.MIN_VOLUME:
                continue
            if m.liquidity < self.MIN_LIQUIDITY:
                continue

            # Cooldown check
            if m.condition_id in self._positions:
                if now - self._positions[m.condition_id] < self.COOLDOWN_PER_MARKET:
                    continue

            scanned += 1

            # Find the favorite side
            p_yes = m.prices.get("yes", 0.5)
            p_no = m.prices.get("no", 0.5)

            if p_yes >= p_no:
                fav_side, fav_price = "YES", p_yes
                token_id = m.tokens.get("yes", "")
            else:
                fav_side, fav_price = "NO", p_no
                token_id = m.tokens.get("no", "")

            # Price filter: only favorites in the sweet spot
            if fav_price < self.MIN_PRICE or fav_price > self.MAX_PRICE:
                continue

            if not token_id:
                continue

            edge = self._estimate_edge(fav_price, m.volume)

            # Only trade if estimated edge is positive after spread
            spread_cost = m.spread / 2 if hasattr(m, 'spread') else 0.01
            net_edge = edge - spread_cost
            if net_edge < 0.01:
                continue

            opp = FavoriteLongshotOpportunity(
                market_id=m.condition_id,
                question=m.question[:80],
                side=fav_side,
                price=fav_price,
                edge=net_edge,
                token_id=token_id,
                volume=m.volume,
                category=m.category,
            )
            opportunities.append(opp)

        # Sort by edge (highest first)
        opportunities.sort(key=lambda o: o.edge, reverse=True)

        # Limit to top N
        remaining_slots = self.MAX_POSITIONS - active
        opportunities = opportunities[:remaining_slots]

        if scanned > 0:
            logger.info(
                f"[FAV-LONG] Scanned {scanned} eligible markets → "
                f"{len(opportunities)} opportunities "
                f"(active={active}/{self.MAX_POSITIONS})"
            )

        return opportunities

    def execute(self, opp: FavoriteLongshotOpportunity, api, risk,
                live: bool = False) -> bool:
        size = self.MAX_BET

        # Kelly sizing: conservative quarter-Kelly
        # f* = (edge) / (1 - price) for favorites
        payoff = (1.0 / opp.price) - 1.0
        if payoff > 0:
            kelly = opp.edge / (1.0 - opp.price)
            quarter_kelly = kelly * 0.25
            # Scale to dollar amount (cap at MAX_BET)
            budget = risk.get_budget("favorite_longshot") if hasattr(risk, 'get_budget') else 500.0
            kelly_size = quarter_kelly * budget
            size = min(max(kelly_size, 10.0), self.MAX_BET)

        if not live:
            self._positions[opp.market_id] = time.time()
            self._total_trades += 1
            logger.info(
                f"[FAV-LONG] PAPER BUY {opp.side} ${size:.0f} @{opp.price:.2f} "
                f"edge={opp.edge:.3f} '{opp.question[:50]}'"
            )
            return True

        try:
            result = api.smart_buy(
                token_id=opp.token_id,
                amount=size,
                target_price=opp.price,
            )
            if result:
                self._positions[opp.market_id] = time.time()
                self._total_trades += 1
                logger.info(
                    f"[FAV-LONG] BUY {opp.side} ${size:.0f} @{opp.price:.2f} "
                    f"edge={opp.edge:.3f} vol=${opp.volume:,.0f} "
                    f"'{opp.question[:50]}'"
                )
                return True
            else:
                logger.warning(f"[FAV-LONG] Order failed: {opp.question[:50]}")
                return False
        except Exception as e:
            logger.warning(f"[FAV-LONG] Error: {e}")
            return False

    @property
    def stats(self) -> dict:
        return {
            "total_trades": self._total_trades,
            "active_positions": len(self._positions),
        }
