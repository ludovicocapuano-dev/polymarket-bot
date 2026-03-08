"""
Abandoned Position Arbitrage v1.0
(The Polymarket Edge, Annoni)

Identifies markets where positions have been abandoned by losing traders,
creating near risk-free opportunities. Abandoned positions inflate
the displayed probability above the true remaining probability.

Signals:
- Market near resolution (< 48h remaining)
- One side priced > $0.94 (near-certainty)
- Very low recent volume (traders have given up)
- Outcome is predictable from available data

Similar to resolution_sniper but focuses on LOW volume markets specifically
where traders have abandoned losing positions, leaving cheap shares on the table.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from utils.risk_manager import Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "abandoned_position"

# Categories where we expect abandonment (long-running markets)
ABANDONMENT_CATEGORIES = {"politics", "geopolitics", "elections", "science", ""}


@dataclass
class AbandonedPositionOpportunity:
    market_id: str
    question: str
    side: str  # "YES" or "NO" — the near-certain side to buy
    price: float  # current price of the near-certain side
    token_id: str
    volume_24h: float
    hours_to_resolution: float
    expected_return_pct: float  # (1.0 - price) * 100


class AbandonedPositionStrategy:
    """
    Buys near-certain outcomes in low-volume markets approaching resolution.

    Edge: when traders abandon losing positions, the near-certain side
    can be bought at $0.94-$0.98 for a 2-6% return on resolution.
    Risk: outcome flips (very rare at >$0.94 with <48h remaining).
    """

    # Price thresholds for "near-certainty"
    MIN_NEAR_CERTAIN_PRICE = 0.94
    MAX_NEAR_CERTAIN_PRICE = 0.99  # don't buy at $0.99+ (1% return not worth it)

    # Volume threshold: low volume = abandoned
    MAX_VOLUME_24H = 500.0  # $500 max daily volume = market is dead

    # Time to resolution
    MAX_HOURS_TO_RESOLUTION = 48.0
    MIN_HOURS_TO_RESOLUTION = 1.0  # avoid last-minute resolution chaos

    # Position sizing
    MAX_POSITION = 50.0
    MIN_POSITION = 10.0

    # Limit order: try to get a better fill
    LIMIT_ORDER_PRICE = 0.95  # place limit at $0.95 minimum

    # Scan interval: every 15 minutes
    SCAN_INTERVAL = 900

    # Cooldown per market (don't re-scan same market)
    MARKET_COOLDOWN = 3600  # 1 hour

    def __init__(self):
        self._last_scan = 0.0
        self._positioned_markets: set[str] = set()
        self._market_cooldowns: dict[str, float] = {}
        self._total_trades = 0
        self._total_invested = 0.0

    def _hours_until_end(self, market) -> float:
        """Calculate hours remaining until market end_date."""
        end_date_str = getattr(market, 'end_date_iso', None) or getattr(market, 'end_date', None)
        if not end_date_str:
            return float('inf')

        try:
            if isinstance(end_date_str, str):
                # Handle ISO format with or without Z
                end_date_str = end_date_str.replace('Z', '+00:00')
                end_dt = datetime.fromisoformat(end_date_str)
            elif isinstance(end_date_str, (int, float)):
                end_dt = datetime.fromtimestamp(end_date_str, tz=timezone.utc)
            else:
                return float('inf')

            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            delta = end_dt - now
            return max(0.0, delta.total_seconds() / 3600.0)
        except (ValueError, TypeError, OSError):
            return float('inf')

    def _get_volume_24h(self, market) -> float:
        """Extract 24h volume from market data."""
        # Try various attribute names used in the codebase
        vol = getattr(market, 'volume_24h', None)
        if vol is not None:
            return float(vol)
        vol = getattr(market, 'volume24hr', None)
        if vol is not None:
            return float(vol)
        # Fallback: use total volume as proxy (less accurate)
        vol = getattr(market, 'volume', 0)
        return float(vol)

    def scan(self, markets: list, existing_positions: set[str] = None) -> list[AbandonedPositionOpportunity]:
        """
        Scan markets for abandoned position opportunities.

        Looks for:
        1. Markets within 48h of resolution
        2. One side priced > $0.94 (near-certainty)
        3. Very low recent volume (< $500/24h)
        """
        now = time.time()
        if now - self._last_scan < self.SCAN_INTERVAL:
            return []
        self._last_scan = now

        if existing_positions:
            self._positioned_markets.update(existing_positions)

        opportunities = []
        scanned = 0
        near_resolution = 0

        for m in markets:
            if not getattr(m, 'active', True):
                continue

            scanned += 1

            # Skip if already positioned
            cid = getattr(m, 'condition_id', '')
            if cid in self._positioned_markets:
                continue

            # Cooldown check
            if cid in self._market_cooldowns:
                if now - self._market_cooldowns[cid] < self.MARKET_COOLDOWN:
                    continue

            # Check time to resolution
            hours_left = self._hours_until_end(m)
            if hours_left > self.MAX_HOURS_TO_RESOLUTION:
                continue
            if hours_left < self.MIN_HOURS_TO_RESOLUTION:
                continue

            near_resolution += 1

            # Check volume (low = abandoned)
            vol_24h = self._get_volume_24h(m)
            if vol_24h > self.MAX_VOLUME_24H:
                continue

            # Check prices — look for near-certain outcome
            p_yes = m.prices.get("yes", 0.5)
            p_no = m.prices.get("no", 0.5)

            # Determine which side is near-certain
            side = None
            price = 0.0
            token_id = ""

            if self.MIN_NEAR_CERTAIN_PRICE <= p_yes <= self.MAX_NEAR_CERTAIN_PRICE:
                side = "YES"
                price = p_yes
                token_id = m.tokens.get("yes", "") if hasattr(m, 'tokens') else ""
            elif self.MIN_NEAR_CERTAIN_PRICE <= p_no <= self.MAX_NEAR_CERTAIN_PRICE:
                side = "NO"
                price = p_no
                token_id = m.tokens.get("no", "") if hasattr(m, 'tokens') else ""

            if not side or not token_id:
                continue

            expected_return = (1.0 - price) * 100.0

            opp = AbandonedPositionOpportunity(
                market_id=cid,
                question=getattr(m, 'question', '')[:80],
                side=side,
                price=price,
                token_id=token_id,
                volume_24h=vol_24h,
                hours_to_resolution=round(hours_left, 1),
                expected_return_pct=round(expected_return, 2),
            )
            opportunities.append(opp)

            # Set cooldown so we don't re-scan immediately
            self._market_cooldowns[cid] = now

        # Sort by expected return (highest first)
        opportunities.sort(key=lambda o: o.expected_return_pct, reverse=True)

        if opportunities:
            logger.info(
                f"[ABANDONED] Scanned {scanned} markets, {near_resolution} near resolution, "
                f"{len(opportunities)} abandoned opportunities "
                f"(best: {opportunities[0].expected_return_pct:.1f}% return)"
            )
        else:
            logger.debug(
                f"[ABANDONED] Scanned {scanned} markets, {near_resolution} near resolution, "
                f"0 abandoned opportunities"
            )

        return opportunities

    def execute(self, opp: AbandonedPositionOpportunity, api, risk, live: bool = False) -> bool:
        """
        Execute an abandoned position trade.

        Uses limit orders at >= $0.95 for better fills.
        """
        # Size: scale with expected return, capped at MAX_POSITION
        size = min(self.MAX_POSITION, max(self.MIN_POSITION, self.MAX_POSITION * opp.expected_return_pct / 6.0))

        # Target price: use the higher of current price and our minimum limit
        target_price = max(opp.price, self.LIMIT_ORDER_PRICE)

        if not live:
            self._positioned_markets.add(opp.market_id)
            self._total_trades += 1
            self._total_invested += size
            logger.info(
                f"[ABANDONED] PAPER BUY {opp.side} ${size:.0f} @{target_price:.3f} "
                f"'{opp.question[:50]}' "
                f"return={opp.expected_return_pct:.1f}% vol24h=${opp.volume_24h:.0f} "
                f"hours_left={opp.hours_to_resolution:.1f}h"
            )
            return True

        try:
            result = api.smart_buy(
                token_id=opp.token_id,
                amount=size,
                target_price=target_price,
            )
            if result:
                self._positioned_markets.add(opp.market_id)
                self._total_trades += 1
                self._total_invested += size
                logger.info(
                    f"[ABANDONED] BUY {opp.side} ${size:.0f} @{target_price:.3f} "
                    f"'{opp.question[:50]}' "
                    f"return={opp.expected_return_pct:.1f}% vol24h=${opp.volume_24h:.0f} "
                    f"hours_left={opp.hours_to_resolution:.1f}h"
                )
                # v12.0.1: register trade in risk manager
                if risk:
                    trade = Trade(
                        timestamp=time.time(),
                        strategy="abandoned_position",
                        market_id=opp.market_id,
                        token_id=opp.token_id,
                        side=f"BUY_{opp.side}",
                        size=size,
                        price=target_price,
                        edge=opp.expected_return_pct / 100,
                        reason="abandoned",
                    )
                    risk.open_trade(trade)
                return True
            else:
                logger.warning(f"[ABANDONED] Order failed: {opp.question[:50]}")
                return False
        except Exception as e:
            logger.warning(f"[ABANDONED] Error: {e}")
            return False

    @property
    def stats(self) -> dict:
        return {
            "total_trades": self._total_trades,
            "total_invested": round(self._total_invested, 2),
            "positioned_markets": len(self._positioned_markets),
        }
