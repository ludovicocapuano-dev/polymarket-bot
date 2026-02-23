"""
Correlation Monitor v9.0 — Limite esposizione per tema.

Max 40% capitale per tema (politics, crypto, weather, geopolitical, sports, finance).
Previene cluster risk: se tutto il portafoglio è su politics e un black swan
politico colpisce, si perde tutto.
"""

import logging
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


# Keyword → tema mapping
THEME_KEYWORDS = {
    "politics": [
        "president", "election", "congress", "senate", "democrat", "republican",
        "trump", "biden", "vote", "poll", "governor", "mayor", "political",
        "party", "cabinet", "impeach", "legislation", "bill", "law",
    ],
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "defi", "nft",
        "blockchain", "token", "solana", "sol", "binance", "coinbase",
        "stablecoin", "altcoin", "mining", "halving",
    ],
    "weather": [
        "temperature", "weather", "rain", "snow", "wind", "celsius",
        "fahrenheit", "forecast", "storm", "hurricane", "tornado",
    ],
    "geopolitical": [
        "war", "conflict", "sanction", "nato", "military", "invasion",
        "ceasefire", "treaty", "diplomacy", "nuclear", "missile",
        "ukraine", "russia", "china", "taiwan", "iran", "israel",
    ],
    "sports": [
        "nba", "nfl", "mlb", "nhl", "premier league", "champions league",
        "world cup", "super bowl", "playoff", "finals",
        "soccer", "football", "basketball", "baseball", "hockey",
        "tennis", "golf", "boxing", "ufc", "mma", "f1",
    ],
    "finance": [
        "fed", "interest rate", "inflation", "gdp", "unemployment",
        "stock", "s&p", "nasdaq", "dow", "treasury", "bond",
        "earnings", "ipo", "merger", "acquisition", "recession",
    ],
}

MAX_THEME_EXPOSURE_PCT = 0.40  # 40% del capitale


class CorrelationMonitor:
    """Monitora esposizione per tema e blocca trade che superano il limite."""

    def __init__(self, risk_manager=None):
        self.risk_manager = risk_manager
        self._market_themes: dict[str, str] = {}  # market_id → theme

    def classify_theme(self, market_id: str, question: str = "",
                       category: str = "", tags: Optional[list[str]] = None) -> str:
        """Classifica un mercato per tema basandosi su question/category/tags."""
        # Cache
        if market_id in self._market_themes:
            return self._market_themes[market_id]

        text = f"{question} {category} {' '.join(tags or [])}".lower()

        best_theme = "other"
        best_score = 0

        for theme, keywords in THEME_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > best_score:
                best_score = score
                best_theme = theme

        self._market_themes[market_id] = best_theme
        return best_theme

    def register_market(self, market_id: str, theme: str):
        """Registra esplicitamente il tema di un mercato."""
        self._market_themes[market_id] = theme

    def get_theme_exposure(self, theme: str) -> float:
        """Calcola l'esposizione corrente su un tema ($)."""
        if not self.risk_manager:
            return 0.0

        exposure = 0.0
        for t in self.risk_manager.open_trades:
            market_theme = self._market_themes.get(t.market_id, "other")
            if market_theme == theme:
                exposure += t.size
        return exposure

    def check_correlation(self, market_id: str, theme: str,
                          size: float) -> tuple[bool, str]:
        """
        Verifica se un trade è permesso rispetto ai limiti per tema.

        Returns: (allowed: bool, reason: str)
        """
        if not self.risk_manager:
            return True, "OK (no risk_manager)"

        total_capital = self.risk_manager.config.total_capital
        max_exposure = total_capital * MAX_THEME_EXPOSURE_PCT

        current_exposure = self.get_theme_exposure(theme)

        if current_exposure + size > max_exposure:
            return False, (
                f"Correlation limit: tema '{theme}' "
                f"esposizione ${current_exposure:.2f} + ${size:.2f} "
                f"> max ${max_exposure:.2f} ({MAX_THEME_EXPOSURE_PCT:.0%} capitale)"
            )

        logger.debug(
            f"[CORRELATION] {theme}: ${current_exposure:.2f} + ${size:.2f} "
            f"= ${current_exposure + size:.2f} / ${max_exposure:.2f} OK"
        )
        return True, "OK"

    def exposure_report(self) -> dict[str, float]:
        """Ritorna esposizione per ogni tema."""
        if not self.risk_manager:
            return {}

        report: dict[str, float] = defaultdict(float)
        for t in self.risk_manager.open_trades:
            theme = self._market_themes.get(t.market_id, "other")
            report[theme] += t.size
        return dict(report)
