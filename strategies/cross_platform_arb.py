"""
Cross-Platform Arbitrage v1.0
(Complete Polymarket Guide)

Compares prediction market prices across platforms for equivalent markets.
When significant price differences exist with identical resolution criteria,
trades the gap.

Platforms monitored:
- Polymarket (primary, trading happens here)
- Metaculus (free API, community forecasts)
- Manifold Markets (free API, play money but good calibration)

Edge: Polymarket has more retail flow; Metaculus/Manifold have better
calibrated crowds. When Polymarket diverges > 8%, trade toward the
calibrated consensus.

Uses the existing CrossPlatformFeed (utils/metaculus_feed.py) for
fetching and matching cross-platform probabilities.
"""

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

STRATEGY_NAME = "cross_platform_arb"

# Fee-free categories where this strategy operates
FEE_FREE_CATEGORIES = {"politics", "geopolitics", "elections", ""}


@dataclass
class CrossPlatformArbOpportunity:
    market_id: str
    question: str
    side: str  # "YES" or "NO" — direction to trade on Polymarket
    polymarket_price: float
    consensus_price: float  # weighted consensus from other platforms
    divergence: float  # absolute divergence
    token_id: str
    platforms_detail: str  # human-readable detail of cross-platform data
    n_platforms: int  # number of platforms that agree
    edge_pct: float  # estimated edge percentage


class CrossPlatformArbStrategy:
    """
    Trades Polymarket price divergences vs calibrated consensus from
    Metaculus and Manifold Markets.

    When Polymarket price diverges > 8% from cross-platform consensus,
    trade toward the consensus. The edge comes from Polymarket's higher
    retail flow creating temporary mispricings that calibrated forecaster
    communities don't share.
    """

    # Minimum divergence to trigger a trade
    MIN_DIVERGENCE = 0.08  # 8%

    # Maximum divergence (too large = different resolution criteria)
    MAX_DIVERGENCE = 0.30  # 30% — beyond this, markets probably aren't equivalent

    # Position sizing
    MAX_POSITION = 40.0
    MIN_POSITION = 10.0

    # Scan interval: every 30 minutes
    SCAN_INTERVAL = 1800

    # Maximum concurrent positions
    MAX_POSITIONS = 8

    # Minimum similarity score from cross-platform matching
    MIN_MATCH_SIMILARITY = 0.50

    # Minimum volume on Polymarket (avoid illiquid markets)
    MIN_POLYMARKET_VOLUME = 10000  # $10K total volume

    # Cooldown per market
    MARKET_COOLDOWN = 7200  # 2 hours

    def __init__(self, cross_platform_feed=None):
        """
        Args:
            cross_platform_feed: CrossPlatformFeed instance from utils/metaculus_feed.py.
                                 If None, strategy is disabled (noop).
        """
        self._feed = cross_platform_feed
        self._last_scan = 0.0
        self._positioned_markets: set[str] = set()
        self._market_cooldowns: dict[str, float] = {}
        self._total_trades = 0
        self._total_invested = 0.0
        self._total_pnl_est = 0.0  # estimated PnL from divergence

    def _is_fee_free(self, market) -> bool:
        """Check if market is in a fee-free category."""
        cat = getattr(market, 'category', '').lower().strip()
        tags = getattr(market, 'tags', [])
        if isinstance(tags, list):
            tag_set = {t.lower().strip() for t in tags if isinstance(t, str)}
        else:
            tag_set = set()

        return cat in FEE_FREE_CATEGORIES or bool(tag_set & FEE_FREE_CATEGORIES)

    def _compute_consensus(self, cross_probs) -> tuple[float, int, str]:
        """
        Compute weighted consensus probability from cross-platform data.

        Weights:
        - Metaculus: 1.2x (expert forecasters, well-calibrated)
        - Manifold: 1.0x (play money but decent calibration)
        - Weight by similarity score

        Returns:
            (consensus_prob, n_platforms, detail_string)
        """
        if not cross_probs:
            return 0.0, 0, ""

        platform_weights = {
            "metaculus": 1.2,
            "manifold": 1.0,
        }

        weighted_sum = 0.0
        total_weight = 0.0
        details = []
        platforms_seen = set()

        for cp in cross_probs:
            if cp.similarity < self.MIN_MATCH_SIMILARITY:
                continue

            base_weight = platform_weights.get(cp.platform, 1.0)
            # Scale weight by similarity (higher similarity = more trust)
            weight = base_weight * cp.similarity

            weighted_sum += cp.probability * weight
            total_weight += weight
            platforms_seen.add(cp.platform)
            details.append(
                f"{cp.platform}={cp.probability:.0%}(sim={cp.similarity:.2f})"
            )

        if total_weight == 0:
            return 0.0, 0, ""

        consensus = weighted_sum / total_weight
        detail = ", ".join(details)
        return consensus, len(platforms_seen), detail

    def scan(self, markets: list, existing_positions: set[str] = None) -> list[CrossPlatformArbOpportunity]:
        """
        Scan Polymarket markets for cross-platform divergences.

        For each eligible market, queries Metaculus and Manifold for
        equivalent markets and checks if prices diverge > 8%.
        """
        if not self._feed:
            return []

        now = time.time()
        if now - self._last_scan < self.SCAN_INTERVAL:
            return []
        self._last_scan = now

        if existing_positions:
            self._positioned_markets.update(existing_positions)

        if len(self._positioned_markets) >= self.MAX_POSITIONS:
            logger.debug(f"[XPLATFORM-ARB] Max positions ({self.MAX_POSITIONS}) reached, skipping scan")
            return []

        opportunities = []
        scanned = 0
        queried = 0

        for m in markets:
            if not getattr(m, 'active', True):
                continue

            # Only fee-free categories
            if not self._is_fee_free(m):
                continue

            # Skip low-volume markets
            vol = getattr(m, 'volume', 0)
            if vol < self.MIN_POLYMARKET_VOLUME:
                continue

            cid = getattr(m, 'condition_id', '')
            if not cid:
                continue

            # Skip if already positioned
            if cid in self._positioned_markets:
                continue

            # Cooldown check
            if cid in self._market_cooldowns:
                if now - self._market_cooldowns[cid] < self.MARKET_COOLDOWN:
                    continue

            scanned += 1

            # Rate-limit cross-platform queries (max 10 per scan cycle)
            if queried >= 10:
                break

            question = getattr(m, 'question', '')
            if not question:
                continue

            p_yes = m.prices.get("yes", 0.5)

            # Query cross-platform feeds
            try:
                cross_probs = self._feed.get_cross_platform_prob(question, p_yes)
                queried += 1
            except Exception as e:
                logger.debug(f"[XPLATFORM-ARB] Feed error for '{question[:40]}': {e}")
                continue

            if not cross_probs:
                continue

            # Compute consensus
            consensus, n_platforms, detail = self._compute_consensus(cross_probs)
            if n_platforms == 0:
                continue

            # Check divergence
            divergence = abs(p_yes - consensus)
            if divergence < self.MIN_DIVERGENCE:
                continue
            if divergence > self.MAX_DIVERGENCE:
                logger.debug(
                    f"[XPLATFORM-ARB] Divergence too large ({divergence:.0%}), "
                    f"markets may not be equivalent: '{question[:50]}'"
                )
                continue

            # Determine trade direction: trade toward consensus
            if p_yes < consensus:
                # Polymarket underprices YES — buy YES
                side = "YES"
                price = p_yes
                token_id = m.tokens.get("yes", "") if hasattr(m, 'tokens') else ""
            else:
                # Polymarket overprices YES — buy NO (which is underpriced)
                side = "NO"
                price = m.prices.get("no", 1.0 - p_yes)
                token_id = m.tokens.get("no", "") if hasattr(m, 'tokens') else ""

            if not token_id:
                continue

            edge_pct = divergence * 100.0

            opp = CrossPlatformArbOpportunity(
                market_id=cid,
                question=question[:80],
                side=side,
                polymarket_price=price,
                consensus_price=consensus,
                divergence=divergence,
                token_id=token_id,
                platforms_detail=detail,
                n_platforms=n_platforms,
                edge_pct=round(edge_pct, 1),
            )
            opportunities.append(opp)

            # Set cooldown
            self._market_cooldowns[cid] = now

        # Sort by divergence (largest edge first)
        opportunities.sort(key=lambda o: o.divergence, reverse=True)

        if opportunities:
            logger.info(
                f"[XPLATFORM-ARB] Scanned {scanned} fee-free markets, queried {queried}, "
                f"{len(opportunities)} divergence opportunities "
                f"(best: {opportunities[0].edge_pct:.1f}% edge, "
                f"{opportunities[0].n_platforms} platforms)"
            )
        else:
            logger.debug(
                f"[XPLATFORM-ARB] Scanned {scanned} fee-free markets, queried {queried}, "
                f"0 divergence opportunities"
            )

        return opportunities

    def execute(self, opp: CrossPlatformArbOpportunity, api, risk, live: bool = False) -> bool:
        """
        Execute a cross-platform arbitrage trade.

        Buys the underpriced side on Polymarket, expecting convergence
        toward the cross-platform consensus.
        """
        # Size: scale with divergence, capped at MAX_POSITION
        # Larger divergence = more confidence = larger position
        size_factor = min(1.0, opp.divergence / 0.15)  # full size at 15%+ divergence
        size = self.MIN_POSITION + (self.MAX_POSITION - self.MIN_POSITION) * size_factor
        size = round(min(size, self.MAX_POSITION), 2)

        if not live:
            self._positioned_markets.add(opp.market_id)
            self._total_trades += 1
            self._total_invested += size
            self._total_pnl_est += size * opp.divergence
            logger.info(
                f"[XPLATFORM-ARB] PAPER BUY {opp.side} ${size:.0f} @{opp.polymarket_price:.3f} "
                f"'{opp.question[:50]}' "
                f"edge={opp.edge_pct:.1f}% consensus={opp.consensus_price:.3f} "
                f"[{opp.platforms_detail}]"
            )
            return True

        try:
            result = api.smart_buy(
                token_id=opp.token_id,
                amount=size,
                target_price=opp.polymarket_price,
            )
            if result:
                self._positioned_markets.add(opp.market_id)
                self._total_trades += 1
                self._total_invested += size
                self._total_pnl_est += size * opp.divergence
                logger.info(
                    f"[XPLATFORM-ARB] BUY {opp.side} ${size:.0f} @{opp.polymarket_price:.3f} "
                    f"'{opp.question[:50]}' "
                    f"edge={opp.edge_pct:.1f}% consensus={opp.consensus_price:.3f} "
                    f"[{opp.platforms_detail}]"
                )
                return True
            else:
                logger.warning(f"[XPLATFORM-ARB] Order failed: {opp.question[:50]}")
                return False
        except Exception as e:
            logger.warning(f"[XPLATFORM-ARB] Error: {e}")
            return False

    @property
    def stats(self) -> dict:
        return {
            "total_trades": self._total_trades,
            "total_invested": round(self._total_invested, 2),
            "est_pnl": round(self._total_pnl_est, 2),
            "positioned_markets": len(self._positioned_markets),
        }
