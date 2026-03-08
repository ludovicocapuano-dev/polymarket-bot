"""
Kyle's Lambda — Adaptive sizing per liquidità mercato.
(Kyle 1985, TQP Ch 19)

Lambda = price impact coefficient.
High lambda = illiquid market = smaller bets.
Low lambda = liquid market = normal/aggressive bets.
"""

import logging
import numpy as np
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LiquidityProfile:
    market_id: str
    lambda_estimate: float  # price impact per $ traded
    spread: float  # current bid-ask spread
    depth: float  # total $ within 2% of mid
    sizing_multiplier: float  # [0.3, 2.0] scale factor for bet sizing


class KyleLambdaEstimator:
    """
    Estimates Kyle's Lambda (price impact) per market from trade history.
    Lambda = Delta_price / (signed_volume)

    Used to scale bet sizes: illiquid markets get smaller bets.
    """

    # Reasonable defaults for Polymarket
    DEFAULT_LAMBDA = 0.001  # 0.1% impact per $1 traded
    MAX_HISTORY = 100

    def __init__(self):
        self._trade_history: dict[str, deque] = {}  # market_id -> [(price_change, volume, sign)]
        self._lambda_cache: dict[str, float] = {}

    def record_trade(self, market_id: str, price_before: float, price_after: float,
                     volume: float, side: int):
        """Record a trade observation for lambda estimation."""
        if market_id not in self._trade_history:
            self._trade_history[market_id] = deque(maxlen=self.MAX_HISTORY)

        price_change = price_after - price_before
        signed_vol = side * volume  # +1 for buy, -1 for sell
        self._trade_history[market_id].append((price_change, signed_vol))

    def estimate_lambda(self, market_id: str) -> float:
        """
        Estimate Kyle's Lambda from trade history.
        Lambda = Cov(Delta_p, signed_vol) / Var(signed_vol)
        """
        history = self._trade_history.get(market_id, deque())
        if len(history) < 10:
            return self.DEFAULT_LAMBDA

        prices = np.array([h[0] for h in history])
        volumes = np.array([h[1] for h in history])

        # Use np.cov for both to ensure consistent ddof=1 (sample statistics)
        cov_matrix = np.cov(prices, volumes)
        var_vol = cov_matrix[1, 1]
        if var_vol <= 0:
            return self.DEFAULT_LAMBDA

        lam = abs(cov_matrix[0, 1] / var_vol)

        self._lambda_cache[market_id] = lam
        return lam

    def get_sizing_multiplier(self, market_id: str, spread: float = 0.0,
                               depth: float = 0.0) -> LiquidityProfile:
        """
        Get sizing multiplier for a market based on its liquidity profile.

        Multiplier in [0.3, 2.0]:
        - Very illiquid (lambda > 0.005): 0.3x
        - Illiquid (lambda > 0.002): 0.5x
        - Normal (lambda ~ 0.001): 1.0x
        - Liquid (lambda < 0.0005): 1.5x
        - Very liquid (lambda < 0.0001): 2.0x
        """
        lam = self.estimate_lambda(market_id)

        if lam > 0.005:
            mult = 0.3
        elif lam > 0.002:
            mult = 0.5
        elif lam > 0.001:
            mult = 1.0
        elif lam > 0.0005:
            mult = 1.5
        else:
            mult = 2.0

        # Also factor in spread and depth if available
        if spread > 0.05:  # >5% spread = very illiquid
            mult *= 0.5
        elif spread > 0.03:
            mult *= 0.7

        if depth > 0 and depth < 100:  # <$100 visible depth
            mult *= 0.5

        mult = max(0.3, min(2.0, mult))

        profile = LiquidityProfile(
            market_id=market_id,
            lambda_estimate=lam,
            spread=spread,
            depth=depth,
            sizing_multiplier=mult,
        )

        return profile
