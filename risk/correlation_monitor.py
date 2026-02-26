"""
Correlation Monitor v10.2 — Portfolio VaR con matrice di covarianza.

MIT 18.S096 Lecture 7 (Abbott): VaR = z * sqrt(w^T * Sigma * w)
MIT 18.S096 Lecture 14 (Kempthorne): CVaR = E[Loss | Loss > VaR]

v9.0: Limite 40% per tema (keyword-based)
v10.2: Portfolio VaR con covarianza stimata + diversification ratio
"""

import logging
import math
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

# ── Correlazioni stimate per tema (MIT Lecture 7: Abbott) ──
# Intra-tema: posizioni nello stesso tema sono correlate
# Inter-tema: posizioni in temi diversi hanno bassa correlazione
# Basato su: prediction markets hanno binary outcomes che clusterizzano per tema
RHO_INTRA = 0.40   # correlazione dentro lo stesso tema
RHO_INTER = 0.10   # correlazione tra temi diversi


class CorrelationMonitor:
    """Monitora esposizione per tema e calcola Portfolio VaR."""

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

    # ── Portfolio VaR con matrice di covarianza (MIT 18.S096) ──────

    def portfolio_var(self, confidence: float = 0.95) -> dict:
        """
        v10.2: Portfolio VaR con matrice di covarianza (MIT 18.S096 Lecture 7).

        Formula: VaR = z * sqrt(w^T * Sigma * w)
        dove:
        - w = vettore esposizioni ($) per posizione
        - Sigma = matrice covarianza stimata (rho * sigma_i * sigma_j)
        - z = 1.645 (95%) o 2.33 (99%)

        Sigma stimata con correlazioni per tema:
        - Stesso tema (rho_intra=0.40): elections correlano con elections
        - Temi diversi (rho_inter=0.10): elections poco correlate con weather

        sigma_i per binary outcome: sqrt(p_i * (1 - p_i)) * size_i

        Returns dict con: portfolio_var, sum_individual_var, diversification_ratio
        """
        if not self.risk_manager or not self.risk_manager.open_trades:
            return {
                "portfolio_var": 0.0,
                "sum_individual_var": 0.0,
                "diversification_ratio": 1.0,
                "n_positions": 0,
            }

        open_trades = self.risk_manager.open_trades
        n = len(open_trades)

        # z-score per confidence level
        z = 1.645 if confidence <= 0.95 else 2.33

        # Calcola sigma (volatilità $) per ogni posizione
        # Per binary outcome: sigma = size * sqrt(p * (1-p))
        sigmas = []
        themes = []
        for t in open_trades:
            p = max(0.01, min(0.99, t.price))  # clamp per evitare sigma=0
            sigma_i = t.size * math.sqrt(p * (1.0 - p))
            sigmas.append(sigma_i)
            themes.append(self._market_themes.get(t.market_id, "other"))

        # VaR individuale (sum, senza correlazione)
        sum_individual_var = z * sum(sigmas)

        # Portfolio variance: w^T * Sigma * w
        # Sigma_ij = rho_ij * sigma_i * sigma_j
        # Calcoliamo la forma quadratica direttamente (O(n^2), n = posizioni aperte)
        portfolio_variance = 0.0
        for i in range(n):
            for j in range(n):
                if i == j:
                    rho = 1.0
                elif themes[i] == themes[j]:
                    rho = RHO_INTRA
                else:
                    rho = RHO_INTER
                portfolio_variance += rho * sigmas[i] * sigmas[j]

        portfolio_var = z * math.sqrt(max(0.0, portfolio_variance))

        # Diversification ratio: quanto il portafoglio beneficia della diversificazione
        # 1.0 = nessun beneficio (tutto correlato), <1.0 = diversificazione riduce rischio
        diversification_ratio = (
            portfolio_var / sum_individual_var if sum_individual_var > 0 else 1.0
        )

        result = {
            "portfolio_var": round(portfolio_var, 2),
            "sum_individual_var": round(sum_individual_var, 2),
            "diversification_ratio": round(diversification_ratio, 4),
            "n_positions": n,
        }

        logger.info(
            f"[PORTFOLIO_VAR] VaR95=${portfolio_var:.2f} "
            f"(individuale=${sum_individual_var:.2f}, "
            f"diversification={diversification_ratio:.2%})"
        )

        return result

    def portfolio_cvar(self, confidence: float = 0.95) -> float:
        """
        v10.2: Portfolio CVaR (Expected Shortfall) — MIT 18.S096 Lecture 14.

        CVaR = E[Loss | Loss > VaR]
        Per normale: CVaR = sigma_portfolio * phi(z) / (1-alpha)
        dove phi(z) è la PDF standard normale al quantile z.
        """
        var_report = self.portfolio_var(confidence)
        portfolio_var_val = var_report["portfolio_var"]

        if portfolio_var_val <= 0:
            return 0.0

        # z e phi(z) per il livello di confidenza
        if confidence <= 0.95:
            z = 1.645
            phi_z = 0.10314  # PDF standard normale a z=1.645
        else:
            z = 2.33
            phi_z = 0.02652  # PDF standard normale a z=2.33

        alpha = confidence
        # sigma_portfolio = portfolio_var / z
        sigma_p = portfolio_var_val / z if z > 0 else 0
        cvar = sigma_p * phi_z / (1.0 - alpha)

        logger.debug(
            f"[PORTFOLIO_CVAR] CVaR{confidence:.0%}=${cvar:.2f} "
            f"(VaR=${portfolio_var_val:.2f})"
        )
        return round(cvar, 2)
