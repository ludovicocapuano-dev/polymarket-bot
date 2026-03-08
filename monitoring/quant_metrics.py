"""
Quant Metrics v1.0 — Lopez de Prado Statistical Tests

PSR: Probabilistic Sharpe Ratio (AFML Ch 14)
DSR: Deflated Sharpe Ratio (AFML Ch 14)
binHR: Strategy Risk / Binary Hit Rate (AFML Ch 15)

These answer: "Are our strategies REALLY profitable, or just lucky?"
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    from scipy import stats as scipy_stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    logging.getLogger(__name__).warning(
        "[QUANT-METRICS] scipy not installed — using pure-Python fallbacks "
        "for normal CDF, skewness, kurtosis, and binomial CDF. "
        "Install scipy for full precision: pip install scipy"
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure-Python fallbacks when scipy is not available
# ---------------------------------------------------------------------------

def _norm_cdf(z: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun 26.2.17)."""
    if HAS_SCIPY:
        return float(scipy_stats.norm.cdf(z))
    # Rational approximation, max error ~7.5e-8
    a1, a2, a3, a4, a5 = (
        0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429,
    )
    p = 0.3275911
    sign = 1.0 if z >= 0 else -1.0
    x = abs(z) / math.sqrt(2.0)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF (rational approximation, Beasley-Springer-Moro)."""
    if HAS_SCIPY:
        return float(scipy_stats.norm.ppf(p))
    if p <= 0:
        return -10.0
    if p >= 1:
        return 10.0
    if p == 0.5:
        return 0.0
    # Rational approximation for 0 < p < 1
    # Peter Acklam's algorithm (accurate to ~1.15e-9)
    a = [
        -3.969683028665376e+01, 2.209460984245205e+02,
        -2.759285104469687e+02, 1.383577518672690e+02,
        -3.066479806614716e+01, 2.506628277459239e+00,
    ]
    b = [
        -5.447609879822406e+01, 1.615858368580409e+02,
        -1.556989798598866e+02, 6.680131188771972e+01,
        -1.328068155288572e+01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01,
        -2.400758277161838e+00, -2.549732539343734e+00,
        4.374664141464968e+00, 2.938163982698783e+00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01,
        2.445134137142996e+00, 3.754408661907416e+00,
    ]
    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)


def _skewness(arr: np.ndarray) -> float:
    """Sample skewness (Fisher)."""
    if HAS_SCIPY:
        return float(scipy_stats.skew(arr))
    n = len(arr)
    if n < 3:
        return 0.0
    mean = np.mean(arr)
    std = np.std(arr, ddof=1)
    if std == 0:
        return 0.0
    m3 = np.mean((arr - mean) ** 3)
    return float(m3 / std ** 3)


def _kurtosis_non_fisher(arr: np.ndarray) -> float:
    """Sample kurtosis (non-Fisher, i.e. normal = 3.0)."""
    if HAS_SCIPY:
        return float(scipy_stats.kurtosis(arr, fisher=False))
    n = len(arr)
    if n < 4:
        return 3.0
    mean = np.mean(arr)
    std = np.std(arr, ddof=1)
    if std == 0:
        return 3.0
    m4 = np.mean((arr - mean) ** 4)
    return float(m4 / std ** 4)


def _binom_cdf(k: int, n: int, p: float) -> float:
    """Binomial CDF: P[X <= k] where X ~ Binomial(n, p)."""
    if HAS_SCIPY:
        return float(scipy_stats.binom.cdf(k, n, p))
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0 if k < n else 1.0
    # Direct summation for small n, log-space for larger
    # Use regularized incomplete beta via continued fraction is complex;
    # for our use case n < 5000 typically, direct log-sum is fine
    total = 0.0
    for i in range(k + 1):
        log_pmf = (
            _log_comb(n, i)
            + i * math.log(p)
            + (n - i) * math.log(1.0 - p)
        )
        total += math.exp(log_pmf)
    return min(total, 1.0)


def _log_comb(n: int, k: int) -> float:
    """Log of binomial coefficient using lgamma."""
    if k < 0 or k > n:
        return -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StrategyRiskReport:
    strategy: str
    n_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    breakeven_precision: float  # p* = -avg_loss / (avg_win - avg_loss)
    prob_failure: float  # P[precision < p*] using binomial
    sharpe: float
    psr: float  # P[SR > 0] adjusted for skew/kurtosis
    is_significantly_profitable: bool  # PSR > 0.95
    is_structurally_viable: bool  # prob_failure < 0.05


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------

def probabilistic_sharpe_ratio(returns: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """
    PSR: probability that observed Sharpe exceeds benchmark, adjusted for
    skewness and kurtosis of returns. (Lopez de Prado, AFML Ch 14)

    PSR(SR*) = Z( (SR - SR*) * sqrt(T-1) / sqrt(1 - gamma3*SR + (gamma4-1)/4 * SR^2) )
    """
    T = len(returns)
    if T < 10:
        return 0.5  # not enough data

    std = float(np.std(returns, ddof=1))
    sr = float(np.mean(returns)) / std if std > 0 else 0.0
    gamma3 = _skewness(returns)
    gamma4 = _kurtosis_non_fisher(returns)

    denom_sq = 1 - gamma3 * sr + (gamma4 - 1) / 4 * sr ** 2
    if denom_sq <= 0:
        return 0.5

    z = (sr - sr_benchmark) * math.sqrt(T - 1) / math.sqrt(denom_sq)
    return _norm_cdf(z)


def deflated_sharpe_ratio(returns: np.ndarray, n_strategies_tested: int = 13) -> float:
    """
    DSR: PSR with benchmark adjusted for multiple testing.
    The more strategies you test, the higher the bar for significance.
    (Lopez de Prado, AFML Ch 14)

    SR* = sqrt(V[SRs]) * ((1-gamma)*Z^{-1}[1 - 1/N] + gamma*Z^{-1}[1 - 1/(N*e)])
    gamma ~ 0.5772 (Euler-Mascheroni constant)
    """
    N = max(n_strategies_tested, 2)
    gamma_em = 0.5772156649

    # Estimate variance of Sharpe ratios across strategies
    # Use the theoretical variance: Var(SR) ~ (1 + SR^2/2) / T
    T = len(returns)
    if T < 10:
        return 0.5

    std = float(np.std(returns, ddof=1))
    sr = float(np.mean(returns)) / std if std > 0 else 0.0
    var_sr = (1 + sr ** 2 / 2) / T
    std_sr = math.sqrt(max(var_sr, 1e-10))

    # Expected max SR under null (multiple testing correction)
    z1 = _norm_ppf(1 - 1.0 / N)
    z2 = _norm_ppf(1 - 1.0 / (N * math.e))
    sr_star = std_sr * ((1 - gamma_em) * z1 + gamma_em * z2)

    return probabilistic_sharpe_ratio(returns, sr_benchmark=sr_star)


def strategy_risk_binhr(wins: int, losses: int, avg_win: float, avg_loss: float) -> tuple[float, float]:
    """
    Calculate break-even precision and probability of structural failure.
    (Lopez de Prado, AFML Ch 15)

    p* = -avg_loss / (avg_win - avg_loss)  [break-even precision]
    P[failure] = P[precision < p*] using binomial CDF

    Returns: (breakeven_precision, prob_failure)
    """
    n = wins + losses
    if n == 0 or avg_win <= 0:
        return 1.0, 1.0

    # avg_loss should be negative
    avg_loss_neg = -abs(avg_loss)

    # Break-even precision
    denom = avg_win - avg_loss_neg  # avg_win + |avg_loss|
    if denom <= 0:
        return 1.0, 1.0

    p_star = -avg_loss_neg / denom  # = |avg_loss| / (avg_win + |avg_loss|)

    # Probability of failure: P[X <= wins | p = p_star]
    # Under the null that true win rate = break-even p_star,
    # what's the probability of seeing this few (or fewer) wins?
    if wins == 0:
        prob_failure = 1.0
    else:
        # P[X < wins | p = p_star] = P[X <= wins-1 | p = p_star]
        prob_failure = 1.0 - _binom_cdf(wins - 1, n, p_star)

    return float(p_star), float(prob_failure)


# ---------------------------------------------------------------------------
# High-level evaluation
# ---------------------------------------------------------------------------

def evaluate_strategy(strategy: str, pnl_list: list[float]) -> Optional[StrategyRiskReport]:
    """
    Full evaluation of a strategy using all three metrics.
    pnl_list: list of PnL values for closed trades (positive = win, negative = loss)
    """
    if len(pnl_list) < 5:
        return None

    returns = np.array(pnl_list)
    wins = returns[returns > 0]
    losses = returns[returns <= 0]

    n_wins = len(wins)
    n_losses = len(losses)
    n_total = n_wins + n_losses

    if n_total == 0:
        return None

    win_rate = n_wins / n_total
    avg_win = float(np.mean(wins)) if n_wins > 0 else 0.0
    avg_loss = float(np.mean(np.abs(losses))) if n_losses > 0 else 0.0

    # Sharpe ratio (per-trade, not annualized)
    std = float(np.std(returns, ddof=1))
    sharpe = float(np.mean(returns)) / std if std > 0 else 0.0

    # PSR
    psr = probabilistic_sharpe_ratio(returns)

    # Strategy Risk
    p_star, prob_failure = strategy_risk_binhr(n_wins, n_losses, avg_win, avg_loss)

    report = StrategyRiskReport(
        strategy=strategy,
        n_trades=n_total,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        breakeven_precision=p_star,
        prob_failure=prob_failure,
        sharpe=sharpe,
        psr=psr,
        is_significantly_profitable=psr > 0.95,
        is_structurally_viable=prob_failure < 0.05,
    )

    logger.info(
        f"[QUANT-RISK] {strategy}: WR={win_rate:.1%} vs BE={p_star:.1%} | "
        f"PSR={psr:.3f} | P(fail)={prob_failure:.3f} | "
        f"{'SIGNIFICANT' if report.is_significantly_profitable else 'NOT SIG'} | "
        f"{'VIABLE' if report.is_structurally_viable else 'AT RISK'}"
    )

    return report


def evaluate_all_strategies(trades_by_strategy: dict[str, list[float]], n_tested: int = 13) -> dict[str, StrategyRiskReport]:
    """
    Evaluate all strategies with DSR correction for multiple testing.
    trades_by_strategy: {strategy_name: [pnl_values]}
    """
    reports = {}

    for name, pnls in trades_by_strategy.items():
        report = evaluate_strategy(name, pnls)
        if report is None:
            continue

        # Apply DSR correction
        returns = np.array(pnls)
        dsr = deflated_sharpe_ratio(returns, n_strategies_tested=n_tested)

        logger.info(
            f"[QUANT-DSR] {name}: SR={report.sharpe:.3f} PSR={report.psr:.3f} "
            f"DSR={dsr:.3f} (corrected for {n_tested} strategies tested)"
        )

        reports[name] = report

    return reports
