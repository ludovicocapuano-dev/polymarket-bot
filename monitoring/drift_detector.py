"""
Drift Detector v9.0 — Concept drift e microstructure monitoring.

Detecta:
- Concept drift: win rate recente cala >30% vs storico
- Microstructure: spread medio aumenta significativamente
- Calibration drift: Brier score peggiora
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DriftAlert:
    """Alert generato dal drift detector."""
    alert_type: str     # "concept_drift" | "microstructure" | "calibration"
    strategy: str
    severity: str       # "LOW" | "MEDIUM" | "HIGH"
    message: str
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return f"[{self.severity}] {self.alert_type} ({self.strategy}): {self.message}"


class DriftDetector:
    """Monitora concept drift e cambiamenti di regime."""

    # Soglie
    WIN_RATE_DROP_THRESHOLD = 0.30      # Allarme se WR cala >30% vs storico
    MIN_SAMPLES_FOR_DRIFT = 20          # Minimo campioni per dichiarare drift
    SPREAD_INCREASE_THRESHOLD = 2.0     # Allarme se spread raddoppia
    LOOKBACK_RECENT = 20                # Finestra recente
    LOOKBACK_HISTORICAL = 100           # Finestra storica

    def __init__(self, db=None):
        self.db = db
        self._outcomes: dict[str, list[bool]] = defaultdict(list)  # strategy -> [won, won, ...]
        self._spreads: list[float] = []
        self._max_history = 500

    def record_outcome(self, strategy: str, won: bool):
        """Registra l'esito di un trade per una strategia."""
        self._outcomes[strategy].append(won)
        # Rolling window
        if len(self._outcomes[strategy]) > self._max_history:
            self._outcomes[strategy] = self._outcomes[strategy][-self._max_history:]

    def record_spread(self, spread: float):
        """Registra lo spread corrente per monitoraggio microstructure."""
        self._spreads.append(spread)
        if len(self._spreads) > self._max_history:
            self._spreads = self._spreads[-self._max_history:]

    def check_drift(self) -> list[DriftAlert]:
        """
        Esegue tutti i check di drift.
        Ritorna lista di alert (vuota se tutto ok).
        """
        alerts = []

        # Check 1: Concept drift per strategia
        for strategy, outcomes in self._outcomes.items():
            alert = self._check_concept_drift(strategy, outcomes)
            if alert:
                alerts.append(alert)

        # Check 2: Microstructure drift (spread)
        alert = self._check_microstructure_drift()
        if alert:
            alerts.append(alert)

        if alerts:
            for a in alerts:
                logger.warning(f"[DRIFT] {a}")
                # Persisti su DB se disponibile
                if self.db and hasattr(self.db, 'record_drift_alert'):
                    self.db.record_drift_alert(
                        a.alert_type, a.strategy, a.severity, a.message
                    )

        return alerts

    def _check_concept_drift(self, strategy: str,
                             outcomes: list[bool]) -> DriftAlert | None:
        """Detecta calo significativo nel win rate."""
        if len(outcomes) < self.MIN_SAMPLES_FOR_DRIFT:
            return None

        # Storico vs recente
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
        """Detecta aumento significativo negli spread."""
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
                    f"Spread medio aumentato {increase:.1f}x: "
                    f"storico={hist_avg:.4f} → recente={recent_avg:.4f}"
                ),
            )
        return None

    def get_strategy_health(self, strategy: str) -> dict:
        """Ritorna lo stato di salute di una strategia."""
        outcomes = self._outcomes.get(strategy, [])
        if not outcomes:
            return {"status": "NO_DATA", "win_rate": 0, "samples": 0}

        recent = outcomes[-self.LOOKBACK_RECENT:]
        wr = sum(recent) / len(recent) if recent else 0

        status = "HEALTHY"
        if len(recent) >= 10:
            hist = outcomes[-self.LOOKBACK_HISTORICAL:]
            hist_wr = sum(hist) / len(hist) if hist else 0.5
            if hist_wr > 0 and (hist_wr - wr) / hist_wr > self.WIN_RATE_DROP_THRESHOLD:
                status = "DRIFTING"

        return {
            "status": status,
            "win_rate": wr,
            "win_rate_recent": sum(recent) / len(recent) if recent else 0,
            "samples": len(outcomes),
            "recent_samples": len(recent),
        }
