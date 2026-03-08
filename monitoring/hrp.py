"""
HRP — Hierarchical Risk Parity (Lopez de Prado, AFML Ch 16)

Data-driven allocation across strategies based on correlation of returns.
OOS variance 42% lower than CLA (Critical Line Algorithm).
"""

import logging
import numpy as np
from scipy.cluster.hierarchy import linkage, leaves_list
from scipy.spatial.distance import squareform
from typing import Optional

logger = logging.getLogger(__name__)


class HRPAllocator:
    """Computes optimal strategy allocation weights using HRP."""

    MIN_TRADES = 15  # minimum closed trades per strategy for HRP

    def __init__(self, strategy_names: list[str]):
        self.strategy_names = strategy_names
        self._last_weights: dict[str, float] = {}

    def compute_weights(self, returns_by_strategy: dict[str, list[float]]) -> dict[str, float]:
        """
        Compute HRP weights from strategy returns.
        returns_by_strategy: {strategy_name: [pnl_per_trade]}
        Returns: {strategy_name: weight} where weights sum to 1.0
        """
        # Filter strategies with enough data
        valid = {k: v for k, v in returns_by_strategy.items()
                 if len(v) >= self.MIN_TRADES and k in self.strategy_names}

        if len(valid) < 2:
            # Not enough data for HRP, return equal weight
            n = len(self.strategy_names)
            return {s: 1.0 / n for s in self.strategy_names}

        # Build return matrix (align to shortest series)
        names = sorted(valid.keys())
        min_len = min(len(valid[s]) for s in names)
        matrix = np.array([valid[s][-min_len:] for s in names])  # (n_strategies, n_trades)

        # Correlation and covariance
        corr = np.corrcoef(matrix)
        cov = np.cov(matrix)

        # Handle NaN/inf
        corr = np.nan_to_num(corr, nan=0.0)
        cov = np.nan_to_num(cov, nan=1.0)
        np.fill_diagonal(corr, 1.0)

        # Step 1: Tree clustering
        dist = np.sqrt(0.5 * (1 - corr))  # correlation distance
        np.fill_diagonal(dist, 0.0)
        dist = np.maximum(dist, 0.0)  # ensure non-negative

        try:
            condensed = squareform(dist, checks=False)
            link = linkage(condensed, method='single')
        except Exception as e:
            logger.warning(f"[HRP] Linkage failed: {e}, using equal weight")
            return {s: 1.0 / len(self.strategy_names) for s in self.strategy_names}

        # Step 2: Quasi-diagonalization
        sort_ix = list(leaves_list(link))

        # Step 3: Recursive bisection
        weights = self._rec_bisect(cov, sort_ix)

        result = {}
        for i, name in enumerate(names):
            result[name] = weights[i]

        # Add zero weight for strategies not in valid set
        for s in self.strategy_names:
            if s not in result:
                result[s] = 0.0

        # Normalize
        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}

        self._last_weights = result

        logger.info(
            f"[HRP] Allocation: " +
            " | ".join(f"{k}={v:.1%}" for k, v in sorted(result.items()) if v > 0.01)
        )

        return result

    def _rec_bisect(self, cov: np.ndarray, sort_ix: list[int]) -> np.ndarray:
        """Recursive bisection for HRP weights."""
        n = len(sort_ix)
        weights = np.ones(cov.shape[0])

        cluster_items = [sort_ix]

        while len(cluster_items) > 0:
            new_items = []
            for subset in cluster_items:
                if len(subset) <= 1:
                    continue
                mid = len(subset) // 2
                left = subset[:mid]
                right = subset[mid:]

                # Cluster variance (inverse-variance allocation)
                cov_left = cov[np.ix_(left, left)]
                cov_right = cov[np.ix_(right, right)]

                var_left = self._cluster_var(cov_left)
                var_right = self._cluster_var(cov_right)

                total_var = var_left + var_right
                if total_var <= 0:
                    alpha = 0.5
                else:
                    alpha = 1.0 - var_left / total_var

                for i in left:
                    weights[i] *= alpha
                for i in right:
                    weights[i] *= (1 - alpha)

                if len(left) > 1:
                    new_items.append(left)
                if len(right) > 1:
                    new_items.append(right)

            cluster_items = new_items

        return weights

    def _cluster_var(self, cov: np.ndarray) -> float:
        """Inverse-variance portfolio variance for a cluster."""
        ivp = 1.0 / np.diag(cov)
        ivp /= ivp.sum()
        return float(ivp @ cov @ ivp)

    @property
    def last_weights(self) -> dict[str, float]:
        return self._last_weights
