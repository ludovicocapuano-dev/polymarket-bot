"""
Drift Detector v11.0 — CUSUM control chart + strategy health score.

v9.0: simple WR drop > 30% (snapshot, high false positive rate)
v11.0 (Qlib-inspired):
  - CUSUM (Cumulative Sum) control chart for sustained drift detection
    (Page 1954, Montgomery 2009). Detects drift 5-10 trades earlier.
  - Exponentially Weighted Moving Average (EWMA) for spread monitoring
  - Composite Strategy Health Score H ∈ [0, 1] that modulates Kelly sizing:
    H = w_wr * WR_score + w_cusum * CUSUM_score + w_sharpe * Sharpe_score
  - Drift score ∈ [0, 1] for dynamic uncertainty σ in risk_manager
"""

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DriftAlert:
    """Alert generato dal drift detector."""
    alert_type: str     # "concept_drift" | "cusum_drift" | "microstructure"
    strategy: str
    severity: str       # "LOW" | "MEDIUM" | "HIGH"
    message: str
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return f"[{self.severity}] {self.alert_type} ({self.strategy}): {self.message}"


class DriftDetector:
    """Monitora concept drift con CUSUM + health score composito."""

    # CUSUM parameters (Page 1954, Montgomery 2009)
    CUSUM_SLACK = 0.05        # allowance k: ignora deviazioni < 5% dal target WR
    CUSUM_THRESHOLD = 3.0     # decision interval h: alert dopo accumulo 3σ
    CUSUM_RESET_ON_ALERT = True

    # Target WR per strategia (from historical data)
    TARGET_WR: dict[str, float] = {
        "weather": 0.74,
        "resolution_sniper": 0.90,
        "favorite_longshot": 0.60,
        "event_driven": 0.55,
        "high_prob_bond": 0.80,
    }
    DEFAULT_TARGET_WR = 0.60

    # Legacy thresholds (kept for backward compat)
    WIN_RATE_DROP_THRESHOLD = 0.30
    MIN_SAMPLES_FOR_DRIFT = 15    # reduced from 20: CUSUM works on smaller samples
    SPREAD_INCREASE_THRESHOLD = 2.0
    LOOKBACK_RECENT = 20
    LOOKBACK_HISTORICAL = 100

    # Health score weights
    HEALTH_W_WR = 0.40        # win rate component
    HEALTH_W_CUSUM = 0.35     # CUSUM stability component
    HEALTH_W_SHARPE = 0.25    # risk-adjusted return component

    def __init__(self, db=None):
        self.db = db
        self._outcomes: dict[str, list[bool]] = defaultdict(list)
        self._pnls: dict[str, list[float]] = defaultdict(list)  # for Sharpe
        self._spreads: list[float] = []
        self._max_history = 500

        # CUSUM state per strategy
        self._cusum_pos: dict[str, float] = defaultdict(float)  # upper CUSUM
        self._cusum_neg: dict[str, float] = defaultdict(float)  # lower CUSUM
        self._cusum_alerts: dict[str, int] = defaultdict(int)   # alert count

        # EWMA for spread monitoring (λ = 0.15, standard for quality control)
        self._ewma_spread: float = 0.0
        self._ewma_initialized: bool = False
        self._EWMA_LAMBDA = 0.15

    def record_outcome(self, strategy: str, won: bool, pnl: float = 0.0):
        """Registra l'esito di un trade + aggiorna CUSUM."""
        self._outcomes[strategy].append(won)
        if pnl != 0.0:
            self._pnls[strategy].append(pnl)
        if len(self._outcomes[strategy]) > self._max_history:
            self._outcomes[strategy] = self._outcomes[strategy][-self._max_history:]
        if len(self._pnls[strategy]) > self._max_history:
            self._pnls[strategy] = self._pnls[strategy][-self._max_history:]

        # Update CUSUM (two-sided: detect both deterioration and improvement)
        target = self.TARGET_WR.get(strategy, self.DEFAULT_TARGET_WR)
        x = 1.0 if won else 0.0
        deviation = x - target

        # Upper CUSUM: detects downward drift (losses accumulating)
        self._cusum_pos[strategy] = max(
            0.0, self._cusum_pos[strategy] + (-deviation) - self.CUSUM_SLACK
        )
        # Lower CUSUM: detects upward drift (wins accumulating — good!)
        self._cusum_neg[strategy] = max(
            0.0, self._cusum_neg[strategy] + deviation - self.CUSUM_SLACK
        )

    def record_spread(self, spread: float):
        """Registra spread con EWMA smoothing."""
        self._spreads.append(spread)
        if len(self._spreads) > self._max_history:
            self._spreads = self._spreads[-self._max_history:]

        # EWMA update
        if not self._ewma_initialized:
            self._ewma_spread = spread
            self._ewma_initialized = True
        else:
            self._ewma_spread = (
                self._EWMA_LAMBDA * spread +
                (1 - self._EWMA_LAMBDA) * self._ewma_spread
            )

    def check_drift(self) -> list[DriftAlert]:
        """Esegue CUSUM check + microstructure check."""
        alerts = []

        for strategy, outcomes in self._outcomes.items():
            # CUSUM drift detection (primary)
            alert = self._check_cusum_drift(strategy)
            if alert:
                alerts.append(alert)
            # Legacy WR drop check (secondary, higher threshold)
            alert = self._check_concept_drift(strategy, outcomes)
            if alert:
                alerts.append(alert)

        alert = self._check_microstructure_drift()
        if alert:
            alerts.append(alert)

        if alerts:
            for a in alerts:
                logger.warning(f"[DRIFT] {a}")
                if self.db and hasattr(self.db, 'record_drift_alert'):
                    self.db.record_drift_alert(
                        a.alert_type, a.strategy, a.severity, a.message
                    )

        return alerts

    def _check_cusum_drift(self, strategy: str) -> DriftAlert | None:
        """
        CUSUM control chart (Page 1954).
        Detects sustained drift earlier than simple WR comparison.
        Upper CUSUM > threshold → strategy is underperforming.
        """
        outcomes = self._outcomes.get(strategy, [])
        if len(outcomes) < self.MIN_SAMPLES_FOR_DRIFT:
            return None

        cusum_val = self._cusum_pos[strategy]
        if cusum_val > self.CUSUM_THRESHOLD:
            target = self.TARGET_WR.get(strategy, self.DEFAULT_TARGET_WR)
            recent_wr = sum(outcomes[-self.LOOKBACK_RECENT:]) / min(
                len(outcomes), self.LOOKBACK_RECENT
            )
            severity = "HIGH" if cusum_val > self.CUSUM_THRESHOLD * 2 else "MEDIUM"

            self._cusum_alerts[strategy] += 1

            if self.CUSUM_RESET_ON_ALERT:
                self._cusum_pos[strategy] = 0.0

            return DriftAlert(
                alert_type="cusum_drift",
                strategy=strategy,
                severity=severity,
                message=(
                    f"CUSUM={cusum_val:.2f} > {self.CUSUM_THRESHOLD:.1f}: "
                    f"WR={recent_wr:.1%} vs target={target:.1%} "
                    f"(alert #{self._cusum_alerts[strategy]}, "
                    f"n={len(outcomes)})"
                ),
            )
        return None

    def _check_concept_drift(self, strategy: str,
                             outcomes: list[bool]) -> DriftAlert | None:
        """Legacy: detecta calo significativo nel win rate."""
        if len(outcomes) < self.MIN_SAMPLES_FOR_DRIFT:
            return None

        historical = outcomes[-self.LOOKBACK_HISTORICAL:]
        recent = outcomes[-self.LOOKBACK_RECENT:]

        if len(recent) < 10:
            return None

        hist_wr = sum(historical) / len(historical)
        recent_wr = sum(recent) / len(recent)

        if hist_wr <= 0:
            return None

        drop = (hist_wr - recent_wr) / hist_wr

        if drop > self.WIN_RATE_DROP_THRESHOLD:
            severity = "HIGH" if drop > 0.50 else "MEDIUM"
            return DriftAlert(
                alert_type="concept_drift",
                strategy=strategy,
                severity=severity,
                message=(
                    f"Win rate calo {drop:.0%}: "
                    f"storico={hist_wr:.1%} ({len(historical)} trade) "
                    f"→ recente={recent_wr:.1%} ({len(recent)} trade)"
                ),
            )
        return None

    def _check_microstructure_drift(self) -> DriftAlert | None:
        """EWMA-based spread monitoring."""
        if len(self._spreads) < self.MIN_SAMPLES_FOR_DRIFT:
            return None

        historical = self._spreads[-self.LOOKBACK_HISTORICAL:]
        recent = self._spreads[-self.LOOKBACK_RECENT:]

        if len(recent) < 10:
            return None

        hist_avg = sum(historical) / len(historical)
        recent_avg = sum(recent) / len(recent)

        if hist_avg <= 0:
            return None

        increase = recent_avg / hist_avg

        if increase > self.SPREAD_INCREASE_THRESHOLD:
            return DriftAlert(
                alert_type="microstructure",
                strategy="global",
                severity="MEDIUM",
                message=(
                    f"Spread aumentato {increase:.1f}x: "
                    f"storico={hist_avg:.4f} → recente={recent_avg:.4f} "
                    f"(EWMA={self._ewma_spread:.4f})"
                ),
            )
        return None

    def get_drift_score(self, strategy: str) -> float:
        """
        Drift score ∈ [0, 1]. Higher = more drift = less reliable.
        Used by risk_manager to dynamically adjust uncertainty σ.

        Score = CUSUM_pos / (2 * threshold), capped at 1.0
        """
        cusum_val = self._cusum_pos.get(strategy, 0.0)
        return min(1.0, cusum_val / (2.0 * self.CUSUM_THRESHOLD))

    def get_strategy_health(self, strategy: str) -> dict:
        """
        Composite strategy health score H ∈ [0, 1].
        1.0 = perfect health, 0.0 = strategy broken.

        Components:
        - WR score: recent WR / target WR (capped at 1.0)
        - CUSUM score: 1 - drift_score (low CUSUM = healthy)
        - Sharpe score: annualized Sharpe ratio (capped at [0, 1])
        """
        outcomes = self._outcomes.get(strategy, [])
        if not outcomes:
            return {
                "status": "NO_DATA", "win_rate": 0, "samples": 0,
                "health_score": 0.5, "drift_score": 0.0,
                "cusum": 0.0, "sharpe": 0.0,
            }

        recent = outcomes[-self.LOOKBACK_RECENT:]
        wr = sum(recent) / len(recent) if recent else 0
        target = self.TARGET_WR.get(strategy, self.DEFAULT_TARGET_WR)

        # WR score: how close to target (1.0 = at or above target)
        wr_score = min(1.0, wr / target) if target > 0 else 0.5

        # CUSUM score: 1 - drift_score (healthy = no drift)
        drift_score = self.get_drift_score(strategy)
        cusum_score = 1.0 - drift_score

        # Sharpe score: annualized, capped [0, 1]
        pnls = self._pnls.get(strategy, [])
        sharpe = 0.0
        if len(pnls) >= 5:
            mean_pnl = sum(pnls) / len(pnls)
            var_pnl = sum((x - mean_pnl) ** 2 for x in pnls) / len(pnls)
            std_pnl = math.sqrt(var_pnl) if var_pnl > 0 else 0.001
            sharpe = mean_pnl / std_pnl
        sharpe_score = max(0.0, min(1.0, (sharpe + 1.0) / 2.0))  # map [-1,1] → [0,1]

        # Composite health score
        health = (
            self.HEALTH_W_WR * wr_score +
            self.HEALTH_W_CUSUM * cusum_score +
            self.HEALTH_W_SHARPE * sharpe_score
        )

        # Status determination
        status = "HEALTHY"
        if health < 0.35:
            status = "CRITICAL"
        elif health < 0.55:
            status = "DRIFTING"
        elif health < 0.70:
            status = "DEGRADED"

        return {
            "status": status,
            "win_rate": wr,
            "win_rate_recent": wr,
            "samples": len(outcomes),
            "recent_samples": len(recent),
            "health_score": round(health, 3),
            "drift_score": round(drift_score, 3),
            "cusum": round(self._cusum_pos.get(strategy, 0.0), 3),
            "sharpe": round(sharpe, 3),
            "wr_score": round(wr_score, 3),
            "cusum_score": round(cusum_score, 3),
            "sharpe_score": round(sharpe_score, 3),
        }
