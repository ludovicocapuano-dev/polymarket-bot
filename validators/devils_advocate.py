"""
Devil's Advocate v9.0 — Contraddittorio deterministico.

Fast-path (<1ms):
- Sport blacklist per bond
- Edge sospetto (>0.20 per non-arb)
- Overconfident senza dati (confidence>0.85, news<0.3)
- Volume troppo basso (<$500)
- Losing streak (3+ loss recenti nella strategia)
"""

import logging
import time

logger = logging.getLogger(__name__)

class DevilsAdvocate:
    """Contraddittorio deterministico per trade sospetti."""

    SPORT_KEYWORDS = [
        "nba", "nfl", "mlb", "nhl", "premier league", "champions league",
        "world cup", "super bowl", "march madness", "playoff", "finals",
        "soccer", "football", "basketball", "baseball", "hockey",
        "tennis", "golf", "boxing", "ufc", "mma", "f1", "formula",
        "olympics", "grand slam", "world series", "stanley cup",
    ]

    MAX_EDGE_NON_ARB = 0.20
    MAX_CONFIDENCE_WITHOUT_NEWS = 0.85
    MIN_NEWS_FOR_HIGH_CONFIDENCE = 0.3
    MIN_VOLUME = 500.0
    MAX_LOSING_STREAK = 3

    def __init__(self, risk_manager=None):
        self.risk_manager = risk_manager

    def challenge(self, signal) -> tuple[bool, str]:
        """
        Controlla se un segnale è sospetto.
        Returns: (flagged: bool, reason: str)
        """
        # Check 1: Sport blacklist per bond
        if signal.strategy == "high_prob_bond":
            question_lower = signal.question.lower()
            category_lower = signal.category.lower() if signal.category else ""
            for kw in self.SPORT_KEYWORDS:
                if kw in question_lower or kw in category_lower:
                    return True, f"Sport blacklist: '{kw}' (Becker: -$17.4M PnL sport)"

        # Check 2: Edge sospetto per non-arb
        if signal.strategy not in ("arb_gabagool", "arbitrage"):
            if signal.edge > self.MAX_EDGE_NON_ARB:
                return True, (
                    f"Edge sospetto: {signal.edge:.4f} > {self.MAX_EDGE_NON_ARB} "
                    f"per {signal.strategy} (troppo bello per essere vero?)"
                )

        # Check 3: Overconfident senza news
        if (signal.confidence > self.MAX_CONFIDENCE_WITHOUT_NEWS and
                signal.news_strength < self.MIN_NEWS_FOR_HIGH_CONFIDENCE and
                signal.signal_type not in ("bond", "weather", "arb")):
            return True, (
                f"Overconfident: confidence={signal.confidence:.2f} "
                f"ma news_strength={signal.news_strength:.2f} < {self.MIN_NEWS_FOR_HIGH_CONFIDENCE}"
            )

        # Check 4: Volume troppo basso
        if 0 < signal.volume < self.MIN_VOLUME:
            return True, f"Volume basso: ${signal.volume:.0f} < ${self.MIN_VOLUME:.0f}"

        # Check 5: Losing streak
        if self.risk_manager:
            recent_losses = self._count_recent_losses(signal.strategy)
            if recent_losses >= self.MAX_LOSING_STREAK:
                return True, (
                    f"Losing streak: {recent_losses} loss recenti "
                    f"per {signal.strategy} (>= {self.MAX_LOSING_STREAK})"
                )

        return False, ""

    def _count_recent_losses(self, strategy: str, lookback: int = 10) -> int:
        """Conta le loss consecutive recenti per una strategia."""
        if not self.risk_manager:
            return 0

        recent = [
            t for t in self.risk_manager.trades
            if t.strategy == strategy and t.result in ("WIN", "LOSS")
        ]
        recent = recent[-lookback:]

        # Conta loss consecutive dalla fine
        streak = 0
        for t in reversed(recent):
            if t.result == "LOSS":
                streak += 1
            else:
                break
        return streak
