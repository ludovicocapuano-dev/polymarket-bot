"""
Tail Risk Agent v10.2 — CVaR (Expected Shortfall) + VaR.

MIT 18.S096 Lecture 14 (Kempthorne): CVaR = E[Loss | Loss > VaR]
Cattura la severità della coda, non solo la soglia.

- Worst-case: tutte le posizioni perdono
- VaR 95%: approssimazione normale binomiale
- CVaR 95%: expected shortfall (media delle perdite oltre VaR)
- Alert se max_loss > 50% capitale
"""

import logging
import math
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TailRiskReport:
    max_loss_scenario: float    # $ persi se TUTTE le posizioni perdono
    var_95: float               # Value at Risk al 95%
    cvar_95: float = 0.0        # v10.2: CVaR (Expected Shortfall) al 95%
    concentrated_positions: list[dict] = field(default_factory=list)  # posizioni > 10% capitale
    risk_level: str = "NORMAL"  # NORMAL, ELEVATED, CRITICAL
    n_positions: int = 0
    total_exposed: float = 0.0
    capital: float = 0.0
    exposure_pct: float = 0.0

    def __str__(self) -> str:
        return (
            f"TailRisk[{self.risk_level}] "
            f"MaxLoss=${self.max_loss_scenario:+.2f} "
            f"VaR95=${self.var_95:.2f} "
            f"CVaR95=${self.cvar_95:.2f} "
            f"Exposed=${self.total_exposed:.2f}/{self.capital:.2f} "
            f"({self.exposure_pct:.1%}) "
            f"Pos={self.n_positions} "
            f"Concentrated={len(self.concentrated_positions)}"
        )


class TailRiskAgent:
    """Analisi tail risk per il portafoglio con CVaR."""

    # Soglie
    CRITICAL_LOSS_PCT = 0.50    # >50% capitale = CRITICAL
    ELEVATED_LOSS_PCT = 0.30    # >30% capitale = ELEVATED
    CONCENTRATION_PCT = 0.10    # >10% capitale in una posizione = concentrata
    ASSUMED_WIN_RATE = 0.60     # WR assunto per VaR

    def __init__(self, risk_manager=None):
        self.risk_manager = risk_manager

    def analyze(self) -> TailRiskReport:
        """Esegue analisi tail risk completa con CVaR."""
        if not self.risk_manager:
            return TailRiskReport(
                max_loss_scenario=0, var_95=0, cvar_95=0, risk_level="NORMAL"
            )

        rm = self.risk_manager
        capital = rm.capital
        open_trades = rm.open_trades

        if not open_trades:
            return TailRiskReport(
                max_loss_scenario=0, var_95=0, cvar_95=0, risk_level="NORMAL",
                capital=capital
            )

        total_exposed = sum(t.size for t in open_trades)

        # Worst-case: tutte le posizioni perdono
        max_loss = -total_exposed

        # VaR 95%: usando distribuzione binomiale con approssimazione normale
        n = len(open_trades)
        avg_size = total_exposed / n if n > 0 else 0

        # E[losses] = n * (1-WR), Std = sqrt(n * WR * (1-WR))
        # VaR95 = E[losses] + 1.645 * Std
        wr = self.ASSUMED_WIN_RATE
        expected_losses = n * (1 - wr)
        std_losses = math.sqrt(n * wr * (1 - wr)) if n > 0 else 0
        var_95_count = expected_losses + 1.645 * std_losses
        var_95 = min(var_95_count * avg_size, total_exposed)

        # ── CVaR 95% (Expected Shortfall) — MIT 18.S096 Lecture 14 ──
        # CVaR = E[Loss | Loss > VaR]
        # Per distribuzione normale: CVaR_alpha = mu + sigma * phi(z_alpha) / (1-alpha)
        # dove phi è la PDF normale e z_alpha = 1.645 per alpha=0.95
        #
        # phi(1.645) = 0.10314 (standard normal PDF at z=1.645)
        # CVaR_95 = mean_loss + sigma * 0.10314 / 0.05 = mean_loss + sigma * 2.063
        PHI_Z95 = 0.10314  # standard normal PDF at z=1.645
        ALPHA = 0.95
        mean_loss_dollars = expected_losses * avg_size
        std_loss_dollars = std_losses * avg_size
        cvar_95 = min(
            mean_loss_dollars + std_loss_dollars * PHI_Z95 / (1.0 - ALPHA),
            total_exposed
        )

        # Posizioni concentrate (>10% capitale)
        concentrated = []
        for t in open_trades:
            pct = t.size / capital if capital > 0 else 0
            if pct > self.CONCENTRATION_PCT:
                concentrated.append({
                    "market_id": t.market_id[:20],
                    "strategy": t.strategy,
                    "size": t.size,
                    "pct_capital": pct,
                })

        # Determina risk level
        loss_pct = abs(max_loss) / capital if capital > 0 else 0
        if loss_pct > self.CRITICAL_LOSS_PCT:
            risk_level = "CRITICAL"
        elif loss_pct > self.ELEVATED_LOSS_PCT:
            risk_level = "ELEVATED"
        else:
            risk_level = "NORMAL"

        report = TailRiskReport(
            max_loss_scenario=max_loss,
            var_95=var_95,
            cvar_95=cvar_95,
            concentrated_positions=concentrated,
            risk_level=risk_level,
            n_positions=n,
            total_exposed=total_exposed,
            capital=capital,
            exposure_pct=total_exposed / capital if capital > 0 else 0,
        )

        logger.info(f"[TAIL_RISK] {report}")
        if risk_level == "CRITICAL":
            logger.warning(
                f"[TAIL_RISK] CRITICAL: max loss ${abs(max_loss):.2f} "
                f"= {loss_pct:.1%} del capitale!"
            )
        if cvar_95 > capital * 0.40:
            logger.warning(
                f"[TAIL_RISK] CVaR95=${cvar_95:.2f} > 40% capitale "
                f"— rischio coda elevato"
            )

        return report
