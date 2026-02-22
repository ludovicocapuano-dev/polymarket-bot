"""
Tail Risk Agent v9.0 — Analisi worst-case e VaR.

- Worst-case: tutte le posizioni perdono
- VaR 95%: ~40% esposizione (assumendo 60% WR)
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
            f"Exposed=${self.total_exposed:.2f}/{self.capital:.2f} "
            f"({self.exposure_pct:.1%}) "
            f"Pos={self.n_positions} "
            f"Concentrated={len(self.concentrated_positions)}"
        )


class TailRiskAgent:
    """Analisi tail risk per il portafoglio."""

    # Soglie
    CRITICAL_LOSS_PCT = 0.50    # >50% capitale = CRITICAL
    ELEVATED_LOSS_PCT = 0.30    # >30% capitale = ELEVATED
    CONCENTRATION_PCT = 0.10    # >10% capitale in una posizione = concentrata
    ASSUMED_WIN_RATE = 0.60     # WR assunto per VaR

    def __init__(self, risk_manager=None):
        self.risk_manager = risk_manager

    def analyze(self) -> TailRiskReport:
        """Esegue analisi tail risk completa."""
        if not self.risk_manager:
            return TailRiskReport(
                max_loss_scenario=0, var_95=0, risk_level="NORMAL"
            )

        rm = self.risk_manager
        capital = rm.capital
        open_trades = rm.open_trades

        if not open_trades:
            return TailRiskReport(
                max_loss_scenario=0, var_95=0, risk_level="NORMAL",
                capital=capital
            )

        total_exposed = sum(t.size for t in open_trades)

        # Worst-case: tutte le posizioni perdono
        max_loss = -total_exposed

        # VaR 95%: usando distribuzione binomiale
        # Con WR=60%, la probabilità di perdere > X posizioni è calcolata
        # Semplificazione: VaR95 ≈ 40% dell'esposizione totale
        # (basato su: con 60% WR, nel 5% worst case perdiamo ~40% extra)
        n = len(open_trades)
        avg_size = total_exposed / n if n > 0 else 0

        # Stima perdite nel 5% worst case usando approssimazione normale
        # E[losses] = n * (1-WR)
        # Std = sqrt(n * WR * (1-WR))
        # VaR95 = E[losses] + 1.645 * Std
        wr = self.ASSUMED_WIN_RATE
        expected_losses = n * (1 - wr)
        std_losses = math.sqrt(n * wr * (1 - wr)) if n > 0 else 0
        var_95_count = expected_losses + 1.645 * std_losses
        var_95 = min(var_95_count * avg_size, total_exposed)

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

        return report
