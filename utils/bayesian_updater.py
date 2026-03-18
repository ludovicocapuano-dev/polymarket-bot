"""
Bayesian Probability Updater (v12.9)
=====================================
Chains evidence updates using Bayes' theorem.
Each new signal nudges the probability proportionally.

P(H|E) = P(E|H) · P(H) / P(E)

Used by:
- Weather: new forecast source → update probability
- Crowd prediction: each agent group → Bayesian update
- UW signals: congress trade → update on related market
- Econ sniper: each data point → update
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def bayes_update(prior: float, likelihood_true: float,
                 likelihood_false: float) -> float:
    """
    Single Bayesian update.

    prior: P(H) — current belief
    likelihood_true: P(E|H) — probability of seeing this evidence if H is true
    likelihood_false: P(E|¬H) — probability of seeing this evidence if H is false

    Returns: P(H|E) — updated belief
    """
    if prior <= 0:
        return 0.0
    if prior >= 1:
        return 1.0

    numerator = likelihood_true * prior
    denominator = numerator + likelihood_false * (1.0 - prior)

    if denominator <= 0:
        return prior

    posterior = numerator / denominator
    return max(0.001, min(0.999, posterior))


def bayes_chain(prior: float, evidence: list[tuple[float, float]]) -> float:
    """
    Chain multiple evidence updates.

    evidence: list of (likelihood_true, likelihood_false) tuples
    Returns: final posterior after all evidence
    """
    p = prior
    for lt, lf in evidence:
        p = bayes_update(p, lt, lf)
    return p


@dataclass
class BayesianTracker:
    """
    Tracks probability of a market hypothesis with sequential evidence updates.
    Maintains audit trail of all updates.
    """
    market_id: str
    hypothesis: str  # e.g., "Temperature in NYC will be 50-51°F"
    prior: float  # initial market price or model estimate
    current: float = 0.0  # current posterior
    updates: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.current == 0:
            self.current = self.prior

    def update(self, evidence_name: str, likelihood_true: float,
               likelihood_false: float, weight: float = 1.0) -> float:
        """
        Update probability with new evidence.

        evidence_name: human-readable label ("WU forecast", "OpenMeteo", etc.)
        likelihood_true: P(evidence | hypothesis true)
        likelihood_false: P(evidence | hypothesis false)
        weight: confidence in this evidence source (0-1). Blends toward prior.

        Returns: updated probability
        """
        old = self.current

        # Apply weight: interpolate likelihood toward uninformative (0.5, 0.5)
        if weight < 1.0:
            lt = likelihood_true * weight + 0.5 * (1 - weight)
            lf = likelihood_false * weight + 0.5 * (1 - weight)
        else:
            lt = likelihood_true
            lf = likelihood_false

        self.current = bayes_update(self.current, lt, lf)

        self.updates.append({
            "evidence": evidence_name,
            "lt": round(lt, 4),
            "lf": round(lf, 4),
            "weight": round(weight, 2),
            "prior": round(old, 4),
            "posterior": round(self.current, 4),
            "shift": round(self.current - old, 4),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.debug(
            f"[BAYES] {self.hypothesis[:40]}: "
            f"{old:.3f} → {self.current:.3f} ({self.current - old:+.3f}) "
            f"on '{evidence_name}'"
        )

        return self.current

    @property
    def total_shift(self) -> float:
        """Total probability shift from prior."""
        return self.current - self.prior

    @property
    def n_updates(self) -> int:
        return len(self.updates)

    def summary(self) -> dict:
        return {
            "market_id": self.market_id,
            "hypothesis": self.hypothesis,
            "prior": self.prior,
            "current": self.current,
            "total_shift": round(self.total_shift, 4),
            "n_updates": self.n_updates,
            "updates": self.updates,
        }


# ── Convenience functions for common evidence types ──────────

def weather_source_evidence(forecast_prob: float, source_accuracy: float = 0.75
                            ) -> tuple[float, float]:
    """
    Convert a weather forecast into Bayesian likelihood pair.

    forecast_prob: probability from this source (e.g., 0.15 for P(YES)=15%)
    source_accuracy: how reliable this source is historically (0-1)

    Returns: (likelihood_true, likelihood_false) for Bayes update
    """
    # If source says P(YES)=15% with 75% accuracy:
    # P(evidence | YES) = source says this AND is correct = 0.15 * 0.75 + (1-0.75) * 0.5
    # P(evidence | NO) = source says this AND is wrong = (1-0.15) * 0.75 + (1-0.75) * 0.5
    lt = forecast_prob * source_accuracy + (1 - source_accuracy) * 0.5
    lf = (1 - forecast_prob) * source_accuracy + (1 - source_accuracy) * 0.5
    return (lt, lf)


def news_evidence(sentiment_score: float, relevance: float = 0.8
                  ) -> tuple[float, float]:
    """
    Convert a news sentiment signal into Bayesian likelihood pair.

    sentiment_score: -1 (very bearish) to +1 (very bullish)
    relevance: how relevant this news is to the market (0-1)
    """
    # Map sentiment [-1, +1] to likelihood
    # +1 sentiment → strongly confirms hypothesis
    # -1 sentiment → strongly disconfirms
    base = 0.5 + sentiment_score * 0.4 * relevance
    lt = max(0.1, min(0.9, base))
    lf = max(0.1, min(0.9, 1.0 - base))
    return (lt, lf)


def insider_evidence(direction: str, strength: float = 0.6
                     ) -> tuple[float, float]:
    """
    Convert insider/congress/whale signal into Bayesian likelihood.

    direction: "BULLISH" or "BEARISH"
    strength: signal strength (0-1)
    """
    if direction == "BULLISH":
        lt = 0.5 + strength * 0.3
        lf = 0.5 - strength * 0.2
    else:
        lt = 0.5 - strength * 0.3
        lf = 0.5 + strength * 0.2
    return (lt, lf)
