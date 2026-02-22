"""
Calibration Engine v9.0 — Suggerimenti automatici parametri.

v1.0: solo suggerimenti (log + eventualmente Telegram).
v2.0 (futuro): A/B shadow mode.

Analizza:
- Brier score per strategia → suggerisce min_edge adjustment
- Alpha decay → suggerisce riduzione Kelly fraction
- Win rate vs edge → suggerisce recalibrazione confidence
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ParameterAdjustment:
    """Suggerimento di modifica parametro."""
    strategy: str
    parameter: str
    old_value: str
    new_value: str
    reason: str

    def __str__(self) -> str:
        return (
            f"{self.strategy}.{self.parameter}: "
            f"{self.old_value} → {self.new_value} ({self.reason})"
        )


class CalibrationEngine:
    """Genera suggerimenti di calibrazione basati su performance."""

    # Soglie per suggerimenti
    BRIER_BAD_THRESHOLD = 0.35      # Brier > 0.35 = cattiva calibrazione
    BRIER_GOOD_THRESHOLD = 0.15     # Brier < 0.15 = ottima calibrazione
    ALPHA_DECAY_THRESHOLD = 0.50    # Alpha < 0.50 = edge in forte calo
    ALPHA_GROWTH_THRESHOLD = 1.30   # Alpha > 1.30 = edge in crescita

    def __init__(self, attribution=None, drift_detector=None, db=None):
        self.attribution = attribution
        self.drift_detector = drift_detector
        self.db = db

    def analyze(self) -> list[ParameterAdjustment]:
        """
        Analizza metriche e genera suggerimenti.
        v1.0: solo suggerimenti loggati, nessuna modifica automatica.
        """
        suggestions = []

        if not self.attribution:
            return suggestions

        # Strategie da analizzare
        strategies = [
            "high_prob_bond", "event_driven", "weather",
            "data_driven", "whale_copy",
        ]

        for strategy in strategies:
            # Check 1: Brier score
            brier = self.attribution.get_brier_score(strategy=strategy)
            if brier > self.BRIER_BAD_THRESHOLD:
                suggestions.append(ParameterAdjustment(
                    strategy=strategy,
                    parameter="min_edge",
                    old_value="current",
                    new_value=f"+0.01 (brier={brier:.3f})",
                    reason=(
                        f"Brier score {brier:.3f} > {self.BRIER_BAD_THRESHOLD} "
                        f"= cattiva calibrazione. Aumentare min_edge per "
                        f"filtrare segnali con meno certezza."
                    ),
                ))
            elif brier < self.BRIER_GOOD_THRESHOLD:
                suggestions.append(ParameterAdjustment(
                    strategy=strategy,
                    parameter="min_edge",
                    old_value="current",
                    new_value=f"-0.005 (brier={brier:.3f})",
                    reason=(
                        f"Brier score {brier:.3f} < {self.BRIER_GOOD_THRESHOLD} "
                        f"= ottima calibrazione. Si puo' ridurre min_edge per "
                        f"catturare piu' opportunita'."
                    ),
                ))

            # Check 2: Alpha decay
            alpha = self.attribution.get_alpha_decay(strategy=strategy)
            if alpha < self.ALPHA_DECAY_THRESHOLD:
                suggestions.append(ParameterAdjustment(
                    strategy=strategy,
                    parameter="kelly_fraction",
                    old_value="current",
                    new_value=f"*0.50 (alpha={alpha:.2f})",
                    reason=(
                        f"Alpha decay {alpha:.2f} < {self.ALPHA_DECAY_THRESHOLD} "
                        f"= edge in forte calo. Dimezzare Kelly fraction "
                        f"per ridurre rischio."
                    ),
                ))
            elif alpha > self.ALPHA_GROWTH_THRESHOLD:
                suggestions.append(ParameterAdjustment(
                    strategy=strategy,
                    parameter="kelly_fraction",
                    old_value="current",
                    new_value=f"*1.20 (alpha={alpha:.2f})",
                    reason=(
                        f"Alpha growth {alpha:.2f} > {self.ALPHA_GROWTH_THRESHOLD} "
                        f"= edge in crescita. Considerare aumento Kelly +20%."
                    ),
                ))

        # Log suggerimenti
        for s in suggestions:
            logger.info(f"[CALIBRATION] {s}")
            # Persisti su DB se disponibile
            if self.db and hasattr(self.db, 'record_calibration'):
                self.db.record_calibration(
                    s.strategy, s.parameter,
                    s.old_value, s.new_value, s.reason,
                )

        return suggestions
