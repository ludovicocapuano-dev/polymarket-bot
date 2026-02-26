"""
Empirical Kelly con Monte Carlo v10.0

Calcola il fattore di adjustment Kelly ottimale per ogni strategia
usando bootstrap resampling dei ritorni storici (RohOnChain approach).

Formula chiave:
    f_empirical = 1 - CV_edge
    CV_edge = std(path_means) / mean(path_means)

Alta incertezza nell'edge → haircut aggressivo al sizing.
Sostituisce la correzione statica con un fattore data-driven.
"""

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:
    np = None
    logger.warning(
        "[EMPIRICAL_KELLY] numpy non disponibile — "
        "Empirical Kelly disabilitato, sizing statico v9.x"
    )


@dataclass
class EmpiricalKellyResult:
    """Risultato del calcolo Monte Carlo per una strategia."""
    strategy: str
    f_empirical: float    # fattore di adjustment [0.0, 1.0]
    cv_edge: float        # coefficient of variation dell'edge
    drawdown_95: float    # 95th percentile max drawdown
    n_trades: int         # trade storici usati
    n_paths: int          # paths MC simulati
    timestamp: float      # quando calcolato


class EmpiricalKelly:
    """
    Empirical Kelly con Monte Carlo — data-driven position sizing.

    Genera bootstrap resampling dei ritorni storici per stimare
    l'incertezza dell'edge e calcolare un fattore di haircut.
    """

    MIN_TRADES = 15               # v10.2: ridotto da 30 — blend 70/30 compensa rumore
    N_PATHS = 10_000              # paths Monte Carlo
    DRAWDOWN_PERCENTILE = 95      # target percentile per max drawdown
    MAX_CACHE_AGE = 3600          # cache valida 1 ora
    RECALC_TRADE_THRESHOLD = 10   # ricalcola dopo 10 nuovi trade chiusi
    RECALC_CYCLE_THRESHOLD = 500  # o dopo 500 cicli bot

    def __init__(self):
        self._cache: dict[str, EmpiricalKellyResult] = {}
        self._last_n_trades: dict[str, int] = {}
        self._last_cycle: dict[str, int] = {}

    def needs_recalc(self, strategy: str, n_trades: int, cycle: int) -> bool:
        """Controlla se serve ricalcolo MC per una strategia."""
        if np is None:
            return False

        if n_trades < self.MIN_TRADES:
            return False

        # Mai calcolato
        if strategy not in self._cache:
            return True

        cached = self._cache[strategy]

        # Cache scaduta
        if time.time() - cached.timestamp > self.MAX_CACHE_AGE:
            return True

        # Nuovi trade chiusi
        last_n = self._last_n_trades.get(strategy, 0)
        if n_trades - last_n >= self.RECALC_TRADE_THRESHOLD:
            return True

        # Cicli trascorsi
        last_cycle = self._last_cycle.get(strategy, 0)
        if cycle - last_cycle >= self.RECALC_CYCLE_THRESHOLD:
            return True

        return False

    def update(self, strategy: str, trades: list, cycle: int) -> "EmpiricalKellyResult | None":
        """
        Ricalcola Monte Carlo per una strategia.

        Args:
            strategy: nome strategia
            trades: lista di Trade objects chiusi (con pnl e size)
            cycle: ciclo corrente del bot

        Returns:
            EmpiricalKellyResult o None se insufficienti dati / numpy mancante
        """
        if np is None:
            return None

        # Filtra trade chiusi con size > 0
        valid = [(t.pnl, t.size) for t in trades if t.size > 0]
        if len(valid) < self.MIN_TRADES:
            return None

        # Calcola returns: r_i = pnl_i / size_i
        returns = np.array([pnl / size for pnl, size in valid], dtype=np.float64)

        # Monte Carlo
        f_empirical, cv_edge, dd_95 = self._run_monte_carlo(returns)

        result = EmpiricalKellyResult(
            strategy=strategy,
            f_empirical=f_empirical,
            cv_edge=cv_edge,
            drawdown_95=dd_95,
            n_trades=len(valid),
            n_paths=self.N_PATHS,
            timestamp=time.time(),
        )

        self._cache[strategy] = result
        self._last_n_trades[strategy] = len(valid)
        self._last_cycle[strategy] = cycle

        logger.info(
            f"[EMPIRICAL_KELLY] {strategy}: f_emp={f_empirical:.3f} "
            f"CV={cv_edge:.3f} DD95={dd_95:.2%} "
            f"(n={len(valid)}, paths={self.N_PATHS})"
        )

        return result

    def get_adjustment_factor(self, strategy: str) -> "float | None":
        """
        Ritorna fattore di adjustment [0.0, 1.0] dalla cache.

        Returns:
            float: fattore moltiplicativo, o None se no data / cache scaduta
        """
        if strategy not in self._cache:
            return None

        cached = self._cache[strategy]

        # Cache scaduta (>2h = doppio di MAX_CACHE_AGE per safety margin)
        if time.time() - cached.timestamp > self.MAX_CACHE_AGE * 2:
            return None

        return cached.f_empirical

    @property
    def report(self) -> dict:
        """Summary di tutti i risultati cached."""
        result = {}
        for strategy, emp in self._cache.items():
            result[strategy] = {
                "f_empirical": round(emp.f_empirical, 4),
                "cv_edge": round(emp.cv_edge, 4),
                "drawdown_95": round(emp.drawdown_95, 4),
                "n_trades": emp.n_trades,
                "n_paths": emp.n_paths,
                "age_seconds": round(time.time() - emp.timestamp, 0),
            }
        return result

    def _run_monte_carlo(self, returns: "np.ndarray") -> tuple[float, float, float]:
        """
        Esegue simulazione Monte Carlo con bootstrap resampling.

        Args:
            returns: array numpy di returns per strategia

        Returns:
            (f_empirical, cv_edge, dd_95)
        """
        n_trades = len(returns)
        rng = np.random.default_rng()

        # 1. Bootstrap: genera (N_PATHS, n_trades) indici random con replacement
        indices = rng.integers(0, n_trades, size=(self.N_PATHS, n_trades))
        sampled_returns = returns[indices]  # (N_PATHS, n_trades)

        # 2. Wealth curves: log1p → cumsum → exp (numericamente stabile)
        log_returns = np.log1p(sampled_returns)
        cum_log_returns = np.cumsum(log_returns, axis=1)
        wealth = np.exp(cum_log_returns)  # (N_PATHS, n_trades)

        # 3. Max drawdown per path
        running_max = np.maximum.accumulate(wealth, axis=1)
        drawdowns = 1.0 - wealth / running_max
        max_drawdowns = np.max(drawdowns, axis=1)  # (N_PATHS,)

        # 4. DD95: 95th percentile
        dd_95 = float(np.percentile(max_drawdowns, self.DRAWDOWN_PERCENTILE))

        # 5. CV_edge: per ogni path calcola mean return
        path_means = np.mean(sampled_returns, axis=1)  # (N_PATHS,)
        mean_of_means = float(np.mean(path_means))

        if mean_of_means <= 0:
            # Edge negativo o zero — massimo haircut
            cv_edge = 1.0
        else:
            std_of_means = float(np.std(path_means))
            cv_edge = min(1.0, max(0.0, std_of_means / mean_of_means))

        # 6. f_empirical = 1 - CV_edge
        f_empirical = 1.0 - cv_edge

        return f_empirical, cv_edge, dd_95
