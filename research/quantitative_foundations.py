"""
Quantitative Foundations for Profitable Prediction Market Trading
=================================================================

Research compilation: Mathematical frameworks, edge detection, and risk management
for systematic prediction market trading.

Sources: Academic papers, quantitative finance research, and empirical market data
from Polymarket, Kalshi, PredictIt, and Iowa Electronic Markets (2013-2025).
"""

import math
from dataclasses import dataclass
from typing import Optional, List, Tuple
import numpy as np


# =============================================================================
# SECTION 1: KELLY CRITERION FOR PREDICTION MARKETS
# =============================================================================

@dataclass
class KellyResult:
    """Result of Kelly criterion calculation."""
    fraction: float          # Optimal fraction of bankroll to bet
    expected_growth: float   # Expected log-growth per bet
    edge: float              # Edge = expected_value - 1
    odds_ratio: float        # Implied odds ratio


def kelly_binary(p_true: float, p_market: float) -> KellyResult:
    """
    Kelly criterion for binary prediction market contracts.

    From Beygelzimer et al. and arXiv:2412.14144:

    For an all-or-nothing contract:
      - Market price = p_market (cost to purchase YES contract paying $1)
      - True probability = p_true (your estimate)
      - YES contract pays (1 - p_market) / p_market if correct

    Utility function:
      U(p_true, p_market, f) = (1 - p_true) * log(1 - f) + p_true * log(1 + f * (1 - p_market) / p_market)

    Optimal Kelly fraction (first derivative = 0):
      f* = (Q - P) / (1 + Q)

    Where:
      Q = p_true / (1 - p_true)      [your odds ratio]
      P = p_market / (1 - p_market)   [market odds ratio]

    IMPORTANT: This formula assumes you BUY YES when p_true > p_market.
    When p_true < p_market, compute Kelly for buying NO instead.

    Parameters
    ----------
    p_true : float
        Your estimated true probability of the event (0, 1).
    p_market : float
        Current market price / implied probability (0, 1).

    Returns
    -------
    KellyResult with:
        fraction: Optimal bet size as fraction of bankroll
        expected_growth: Expected log-growth rate per bet
        edge: Your edge (expected profit per dollar risked)
        odds_ratio: Market odds ratio
    """
    if not (0 < p_true < 1 and 0 < p_market < 1):
        raise ValueError("Probabilities must be in (0, 1)")

    # Determine direction: buy YES or buy NO
    if p_true > p_market:
        # Buy YES contract at price p_market
        Q = p_true / (1 - p_true)
        P = p_market / (1 - p_market)
        f_star = (Q - P) / (1 + Q)
        # Edge: expected return per dollar
        edge = p_true * (1 / p_market) + (1 - p_true) * 0 - 1  # = p_true/p_market - 1
        # Expected log growth at optimal fraction
        if f_star > 0:
            growth = ((1 - p_true) * math.log(1 - f_star) +
                      p_true * math.log(1 + f_star * (1 - p_market) / p_market))
        else:
            growth = 0.0
    else:
        # Buy NO contract at price (1 - p_market)
        p_no_true = 1 - p_true
        p_no_market = 1 - p_market
        Q = p_no_true / (1 - p_no_true)
        P = p_no_market / (1 - p_no_market)
        f_star = (Q - P) / (1 + Q)
        edge = p_no_true / p_no_market - 1
        if f_star > 0:
            growth = ((1 - p_no_true) * math.log(1 - f_star) +
                      p_no_true * math.log(1 + f_star * (1 - p_no_market) / p_no_market))
        else:
            growth = 0.0

    f_star = max(f_star, 0.0)  # Never bet negative

    return KellyResult(
        fraction=f_star,
        expected_growth=growth,
        edge=edge,
        odds_ratio=P,
    )


def fractional_kelly(p_true: float, p_market: float,
                     kelly_fraction: float = 0.25,
                     uncertainty_std: float = 0.0) -> KellyResult:
    """
    Fractional Kelly with uncertainty adjustment.

    RATIONALE (from Thorp 2006, Downey 2023, Chu & Swartz 2024):
    Full Kelly maximizes long-run growth but has extreme variance:
      - 1/3 chance of halving bankroll before doubling it
      - 1/n chance of bankroll falling to 1/n at some point

    Fractional Kelly: bet f* = k * f_kelly, where 0 < k < 1.

    WITH UNCERTAINTY (Modified Kelly):
    When p_true is estimated with uncertainty sigma, the effective Kelly
    fraction should be reduced. Using Beta prior on p_true:

      f_adjusted = f_kelly * (1 - sigma^2 / (p_true * (1 - p_true)))

    This accounts for the fact that variance in probability estimates
    makes the Kelly fraction too aggressive. The reduction is proportional
    to (uncertainty / intrinsic_variance).

    PRACTICAL RECOMMENDATIONS:
      - Full Kelly (k=1.0): Maximum growth, extreme volatility
      - Half Kelly (k=0.5): ~75% of growth rate, ~50% of variance
      - Quarter Kelly (k=0.25): ~44% of growth rate, ~25% of variance
      - With 10% probability uncertainty: additional ~20% reduction

    Parameters
    ----------
    p_true : float
        Estimated true probability.
    p_market : float
        Market price.
    kelly_fraction : float
        Fraction of Kelly to use (default 0.25 = quarter Kelly).
    uncertainty_std : float
        Standard deviation of your probability estimate.
    """
    result = kelly_binary(p_true, p_market)

    # Uncertainty adjustment (from Modified Kelly Criteria, Chu & Swartz)
    if uncertainty_std > 0:
        intrinsic_var = p_true * (1 - p_true)
        uncertainty_factor = max(0, 1 - (uncertainty_std ** 2) / intrinsic_var)
    else:
        uncertainty_factor = 1.0

    adjusted_fraction = result.fraction * kelly_fraction * uncertainty_factor

    return KellyResult(
        fraction=adjusted_fraction,
        expected_growth=result.expected_growth * kelly_fraction * uncertainty_factor,
        edge=result.edge,
        odds_ratio=result.odds_ratio,
    )


def portfolio_kelly(
    p_true: List[float],
    p_market: List[float],
    correlation_matrix: Optional[np.ndarray] = None,
    kelly_fraction: float = 0.25,
) -> np.ndarray:
    """
    Portfolio Kelly for multiple simultaneous prediction market bets.

    KEY INSIGHT (from Vegapit, Kelly Wikipedia):
    For simultaneous bets, optimal allocations are SMALLER than sequential
    Kelly, and the distribution across bets is drastically different.

    For N simultaneous binary bets, maximize:
      E[log(W)] = E[log(1 + sum_i f_i * R_i)]

    where R_i is the return of bet i (+gain or -1 for loss).

    With correlation, this requires numerical optimization.
    Without correlation (independent bets), a reasonable approximation:
      f_i_portfolio ≈ f_i_kelly / sqrt(N)  [for large N]

    More precisely, for independent bets, solve:
      max sum_{outcomes} P(outcome) * log(1 + sum_i f_i * r_i(outcome))

    Parameters
    ----------
    p_true : list of float
        True probabilities for each bet.
    p_market : list of float
        Market prices for each bet.
    correlation_matrix : optional array
        NxN correlation matrix between bet outcomes.
    kelly_fraction : float
        Fractional Kelly multiplier.

    Returns
    -------
    np.ndarray of optimal fractions for each bet.
    """
    n = len(p_true)
    assert len(p_market) == n

    # Individual Kelly fractions
    individual_kellys = []
    for i in range(n):
        result = kelly_binary(p_true[i], p_market[i])
        individual_kellys.append(result.fraction)

    individual_kellys = np.array(individual_kellys)

    if correlation_matrix is None:
        # Independent bets: use diversification adjustment
        # For N independent bets, total variance scales as N * avg(f^2)
        # Reduce each by approximately 1/sqrt(N) for similar total risk
        # Then apply fractional Kelly
        if n > 1:
            adjustment = 1.0 / math.sqrt(n)
        else:
            adjustment = 1.0
        return individual_kellys * adjustment * kelly_fraction
    else:
        # With correlations: simple quadratic approximation
        # f_portfolio ≈ Sigma^(-1) * mu, scaled by kelly_fraction
        # where mu_i = edge_i and Sigma = correlation-adjusted variance
        edges = np.array([
            p_true[i] / p_market[i] - 1 if p_true[i] > p_market[i]
            else (1 - p_true[i]) / (1 - p_market[i]) - 1
            for i in range(n)
        ])
        try:
            sigma_inv = np.linalg.inv(correlation_matrix)
            raw_fractions = sigma_inv @ edges
            # Clip negatives and apply fractional Kelly
            raw_fractions = np.maximum(raw_fractions, 0)
            return raw_fractions * kelly_fraction
        except np.linalg.LinAlgError:
            return individual_kellys * kelly_fraction / n


# =============================================================================
# SECTION 2: ARBITRAGE DETECTION
# =============================================================================

@dataclass
class ArbitrageOpportunity:
    """Detected arbitrage in prediction markets."""
    type: str               # 'buy_all', 'sell_all', 'cross_platform', 'combinatorial'
    profit_per_dollar: float # Risk-free profit per dollar invested
    market_ids: list         # Involved markets
    description: str


def detect_single_market_arbitrage(
    outcome_prices: List[float],
    fee_rate: float = 0.02,
) -> Optional[ArbitrageOpportunity]:
    """
    Detect intra-market arbitrage (buy-all or sell-all).

    EMPIRICAL FINDING (arXiv:2508.03474, April 2024 - April 2025):
      - $40M in arbitrage profits extracted from Polymarket
      - 40.9% of conditions had arbitrage opportunities
      - Single-market long: $5.9M profits
      - Single-market short: $4.7M profits
      - Multi-condition buying YES: $11.1M profits
      - Multi-condition buying NO: $17.3M profits
      - Top individual arbitrageur: $2.0M profit

    MATHEMATICAL CONDITION:
      Long arbitrage: sum(prices) < 1 - fees
        Buy one share of every outcome. Guaranteed $1 payout.
        Profit = 1 - sum(prices) - fees

      Short arbitrage: sum(prices) > 1 + fees
        Sell one share of every outcome. Guaranteed $1 liability.
        Profit = sum(prices) - 1 - fees

    MINIMUM THRESHOLD: Research used $0.05/dollar minimum to filter noise.

    Parameters
    ----------
    outcome_prices : list of float
        Current YES prices for all mutually exclusive outcomes.
    fee_rate : float
        Total round-trip fee rate (default 2%).
    """
    total = sum(outcome_prices)

    if total < 1.0 - fee_rate:
        profit = 1.0 - total - fee_rate
        return ArbitrageOpportunity(
            type='buy_all',
            profit_per_dollar=profit / total,
            market_ids=[],
            description=f"Buy all outcomes for ${total:.4f}, receive $1.00. "
                       f"Profit: ${profit:.4f} ({profit/total*100:.2f}% return)"
        )
    elif total > 1.0 + fee_rate:
        profit = total - 1.0 - fee_rate
        return ArbitrageOpportunity(
            type='sell_all',
            profit_per_dollar=profit / 1.0,
            market_ids=[],
            description=f"Sell all outcomes for ${total:.4f}, liability $1.00. "
                       f"Profit: ${profit:.4f} ({profit*100:.2f}% return)"
        )
    return None


def detect_cross_platform_arbitrage(
    price_platform_a: float,
    price_platform_b: float,
    fee_a: float = 0.02,
    fee_b: float = 0.02,
) -> Optional[ArbitrageOpportunity]:
    """
    Detect cross-platform arbitrage for same event.

    EMPIRICAL FINDING (Clinton & Huang 2025):
      - Prices for identical contracts diverged across exchanges
      - Arbitrage opportunities peaked in final 2 weeks before 2024 election
      - PredictIt 2016: up to $0.55/contract arbitrage (55% profit)

    CAUTION: Different platforms may have different settlement rules.
    Polymarket uses UMA Optimistic Oracle; Kalshi uses CFTC-regulated settlement.
    This creates "settlement risk" that isn't true risk-free arbitrage.

    CONDITION:
      Buy YES on platform with lower price, buy NO on platform with higher price.
      Profit if: price_low + (1 - price_high) < 1 - fees
      Simplified: price_high - price_low > fee_a + fee_b
    """
    spread = abs(price_platform_a - price_platform_b)
    total_fees = fee_a + fee_b

    if spread > total_fees:
        profit = spread - total_fees
        if price_platform_a < price_platform_b:
            desc = f"Buy YES on A at {price_platform_a:.3f}, Buy NO on B at {1-price_platform_b:.3f}"
        else:
            desc = f"Buy YES on B at {price_platform_b:.3f}, Buy NO on A at {1-price_platform_a:.3f}"
        return ArbitrageOpportunity(
            type='cross_platform',
            profit_per_dollar=profit,
            market_ids=[],
            description=f"{desc}. Spread: {spread:.3f}, Fees: {total_fees:.3f}, "
                       f"Net profit: {profit:.3f}/dollar"
        )
    return None


# =============================================================================
# SECTION 3: CALIBRATION AND EDGE DETECTION
# =============================================================================

@dataclass
class CalibrationEdge:
    """Detected calibration-based edge."""
    price_bucket: Tuple[float, float]
    market_frequency: float      # How often market prices in this bucket resolve YES
    theoretical_midpoint: float  # Midpoint of price bucket
    edge: float                  # Frequency - midpoint (positive = buy YES is profitable)
    sample_size: int
    confidence_95: Tuple[float, float]  # 95% CI for edge


def compute_calibration_edge(
    prices: List[float],
    outcomes: List[int],
    n_buckets: int = 10,
) -> List[CalibrationEdge]:
    """
    Compute calibration curve and detect systematic mispricings.

    EMPIRICAL FINDINGS (Page & Clemen 2013, multiple studies):

    Favorite-Longshot Bias in prediction markets:
      - Events priced at 80% resolve YES only ~84% of the time (6pts under-calibrated)
      - Events priced at 10-20% resolve YES ~15-25% of the time (longshots overpriced)
      - Events priced at 90%+ resolve YES ~93% of the time (favorites underpriced)

    Time-horizon effects:
      - Short-term markets (< 1 month): reasonably well calibrated
      - Long-term markets (> 3 months): biased toward 50% (compression bias)
        This is caused by time-preference of traders (unwillingness to lock capital)

    The profitable strategy is:
      - Systematically buy favorites (high-probability outcomes) at slight discount
      - Systematically sell longshots (low-probability outcomes) at premium
      - Expected edge: 2-6 percentage points depending on market and time horizon

    REQUIRED SAMPLE SIZE:
      To detect a 3% calibration edge at 95% confidence:
        n >= (z^2 * p * (1-p)) / e^2
        For p=0.80, e=0.03: n >= (1.96^2 * 0.80 * 0.20) / 0.03^2 ≈ 683 observations

    Parameters
    ----------
    prices : list of float
        Market prices at time of observation.
    outcomes : list of int
        Binary outcomes (0 or 1).
    n_buckets : int
        Number of price buckets.
    """
    if len(prices) != len(outcomes):
        raise ValueError("prices and outcomes must have same length")

    bucket_edges = np.linspace(0, 1, n_buckets + 1)
    results = []

    for i in range(n_buckets):
        lo, hi = bucket_edges[i], bucket_edges[i + 1]
        midpoint = (lo + hi) / 2

        mask = [(lo <= p < hi) for p in prices]
        bucket_outcomes = [o for o, m in zip(outcomes, mask) if m]

        if len(bucket_outcomes) < 10:
            continue

        n = len(bucket_outcomes)
        freq = sum(bucket_outcomes) / n
        edge = freq - midpoint

        # Wilson score interval for 95% CI
        z = 1.96
        denominator = 1 + z**2 / n
        center = (freq + z**2 / (2 * n)) / denominator
        spread = z * math.sqrt((freq * (1 - freq) + z**2 / (4 * n)) / n) / denominator

        results.append(CalibrationEdge(
            price_bucket=(lo, hi),
            market_frequency=freq,
            theoretical_midpoint=midpoint,
            edge=edge,
            sample_size=n,
            confidence_95=(center - spread, center + spread),
        ))

    return results


def bayesian_probability_update(
    prior: float,
    likelihood_ratio: float,
) -> float:
    """
    Bayesian update for prediction market probability.

    FRAMEWORK:
    Given prior probability P(H) and new evidence E:
      P(H|E) = P(H) * P(E|H) / P(E)

    Using likelihood ratio LR = P(E|H) / P(E|~H):
      posterior_odds = prior_odds * LR
      P(H|E) = (prior / (1-prior) * LR) / (1 + prior / (1-prior) * LR)

    PRACTICAL APPLICATION:
    1. Start with market price as prior (or your own estimate)
    2. When new information arrives, compute likelihood ratio
    3. Update probability and compare to market price
    4. If |updated - market| > threshold, trade

    EXAMPLE:
      Market: 60% for candidate winning
      New poll: 55% (likelihood ratio vs prior ~0.85)
      Updated: ~55.6%
      If market still at 60%, sell at 4.4% edge

    Parameters
    ----------
    prior : float
        Prior probability.
    likelihood_ratio : float
        P(evidence|hypothesis) / P(evidence|~hypothesis).
    """
    if not (0 < prior < 1):
        raise ValueError("Prior must be in (0, 1)")
    if likelihood_ratio <= 0:
        raise ValueError("Likelihood ratio must be positive")

    prior_odds = prior / (1 - prior)
    posterior_odds = prior_odds * likelihood_ratio
    posterior = posterior_odds / (1 + posterior_odds)
    return posterior


# =============================================================================
# SECTION 4: MEAN REVERSION AND MOMENTUM
# =============================================================================

def compute_autocorrelation(price_changes: List[float], lag: int = 1) -> float:
    """
    Compute autocorrelation of price changes at given lag.

    EMPIRICAL FINDINGS (Clinton & Huang 2025):
      - 58% of Polymarket national presidential markets showed NEGATIVE
        serial correlation (mean reversion on daily timeframe)
      - Price spike one day typically reversed the next
      - This is consistent with noise trading and overreaction

    GENERAL PATTERN (from equity market research, applicable to prediction markets):
      - Intraday to daily: Negative autocorrelation (mean reversion)
        => Contrarian strategies profitable
      - 1-4 weeks: Mixed, depends on market liquidity
      - 1-12 months: Some positive autocorrelation (momentum) in equity markets
        (less documented in prediction markets)

    STRATEGY IMPLICATION:
      If autocorrelation < 0 (mean reversion):
        - Fade large price moves (buy after drops, sell after spikes)
        - Expected edge ≈ |autocorrelation| * average_move_size
        - Requires: sufficient liquidity to execute, low fees

      If autocorrelation > 0 (momentum):
        - Follow trends (buy after rises, sell after drops)
        - Less common in prediction markets than traditional markets

    DETECTION:
      Use variance ratio test: VR(q) = Var(q-period returns) / (q * Var(1-period returns))
      VR > 1 => momentum; VR < 1 => mean reversion
    """
    arr = np.array(price_changes)
    n = len(arr)
    if n <= lag:
        return 0.0

    mean = np.mean(arr)
    var = np.var(arr)
    if var == 0:
        return 0.0

    shifted = arr[lag:] - mean
    original = arr[:n - lag] - mean
    autocorr = np.sum(shifted * original) / (n * var)
    return float(autocorr)


def variance_ratio_test(prices: List[float], q: int = 5) -> Tuple[float, float]:
    """
    Variance ratio test for random walk hypothesis.

    VR(q) = Var(r_t(q)) / (q * Var(r_t(1)))

    Where r_t(q) is the q-period log return.

    VR = 1: Random walk (no predictability)
    VR < 1: Mean reversion (negative autocorrelation)
    VR > 1: Momentum (positive autocorrelation)

    Returns (variance_ratio, z_statistic).
    z_stat > 1.96 or < -1.96 indicates significance at 95%.
    """
    log_prices = np.log(np.array(prices))
    n = len(log_prices)

    # 1-period returns
    r1 = np.diff(log_prices)
    var1 = np.var(r1, ddof=1)

    # q-period returns
    rq = log_prices[q:] - log_prices[:-q]
    varq = np.var(rq, ddof=1)

    if var1 == 0:
        return 1.0, 0.0

    vr = varq / (q * var1)

    # Asymptotic z-statistic under null of random walk
    # z = (VR - 1) / sqrt(2(2q-1)(q-1) / (3q*n))
    z = (vr - 1) / math.sqrt(2 * (2 * q - 1) * (q - 1) / (3 * q * n))

    return float(vr), float(z)


# =============================================================================
# SECTION 5: RISK OF RUIN
# =============================================================================

def risk_of_ruin_fixed_fraction(
    win_prob: float,
    win_amount: float,
    loss_amount: float,
    ruin_threshold: float = 0.1,
) -> float:
    """
    Risk of ruin for fixed-fraction betting.

    GAMBLER'S RUIN FORMULA (for fixed bet sizes):
      If p > q (positive edge):
        P(ruin) = (q/p)^(bankroll/bet_size)

    FOR KELLY BETTING:
      - Under true Kelly, P(halving before doubling) = 1/3
      - P(bankroll falling to 1/n at any point) = 1/n
      - True Kelly has zero long-run ruin probability (bankroll → ∞)
      - But short-run drawdowns can be extreme

    FOR FRACTIONAL KELLY at fraction k:
      - Expected growth rate: g(k) ≈ k * edge - k^2 * variance / 2
      - Growth is maximized at k=1 (full Kelly)
      - Risk of large drawdown decreases rapidly with smaller k
      - At half-Kelly: ~75% of growth, ~50% of variance
      - At quarter-Kelly: ~44% of growth, ~25% of variance

    MONTE CARLO is preferred for realistic risk-of-ruin with:
      - Variable bet sizes
      - Correlated outcomes
      - Time-varying edge
      - Transaction costs

    Parameters
    ----------
    win_prob : float
        Probability of winning each bet.
    win_amount : float
        Amount won (as fraction of bet).
    loss_amount : float
        Amount lost (as fraction of bet, positive number).
    ruin_threshold : float
        Fraction of bankroll considered "ruin" (default 10% = 90% loss).
    """
    if win_prob >= 1 or win_prob <= 0:
        raise ValueError("win_prob must be in (0, 1)")

    q = 1 - win_prob
    p = win_prob

    # Expected value per bet
    ev = p * win_amount - q * loss_amount
    if ev <= 0:
        return 1.0  # Negative edge = certain ruin

    # Approximate using geometric random walk
    # log-growth per bet: p*log(1+w) + q*log(1-l)
    if win_amount >= 1 or loss_amount >= 1:
        # Can't take log of non-positive
        # Use simple gambler's ruin approximation
        if p * win_amount == q * loss_amount:
            return 1.0
        ratio = (q * loss_amount) / (p * win_amount)
        n_units = math.log(1 / ruin_threshold) / math.log(1 / ratio) if ratio < 1 else float('inf')
        return ratio ** max(n_units, 1)
    else:
        growth = p * math.log(1 + win_amount) + q * math.log(1 - loss_amount)
        if growth <= 0:
            return 1.0
        # P(reaching ruin_threshold) ≈ ruin_threshold^(2*growth/variance)
        variance = p * math.log(1 + win_amount)**2 + q * math.log(1 - loss_amount)**2
        if variance == 0:
            return 0.0
        exponent = 2 * growth / variance
        return ruin_threshold ** exponent


def max_drawdown_probability(kelly_fraction: float, drawdown_pct: float) -> float:
    """
    Probability of experiencing a given drawdown under Kelly betting.

    Under Kelly betting, the probability of your bankroll ever falling
    to fraction f of its peak is approximately f^(1/k), where k is the
    Kelly fraction being used.

    For full Kelly (k=1): P(50% drawdown) = 50%
    For half Kelly (k=0.5): P(50% drawdown) = 25%
    For quarter Kelly (k=0.25): P(50% drawdown) = 6.25% (approx)

    More precisely, for a Kelly bettor with fraction k:
      P(bankroll drops to fraction d of peak) ≈ d^(1/k - 1)

    Parameters
    ----------
    kelly_fraction : float
        Fraction of Kelly being used (0 < k <= 1).
    drawdown_pct : float
        Drawdown as a fraction (e.g., 0.5 for 50% drawdown).
    """
    if kelly_fraction <= 0 or kelly_fraction > 1:
        raise ValueError("kelly_fraction must be in (0, 1]")
    if drawdown_pct <= 0 or drawdown_pct >= 1:
        raise ValueError("drawdown_pct must be in (0, 1)")

    # Remaining fraction after drawdown
    remaining = 1 - drawdown_pct
    exponent = (1 / kelly_fraction) - 1
    return remaining ** exponent


# =============================================================================
# SECTION 6: MARKET MANIPULATION DETECTION
# =============================================================================

def manipulation_vulnerability_score(
    daily_volume: float,
    n_traders: int,
    has_external_price: bool,
    n_comments: int,
    liquidity_depth: float,
) -> float:
    """
    Score how vulnerable a market is to manipulation.

    EMPIRICAL FINDINGS (arXiv:2503.03312, 817-market experiment on Manifold):
      - 5 percentage point price shocks persisted 60+ days
      - Only ~40% reversion from initial shock
      - Reversion was fast in first week, then slowed dramatically

    FACTORS REDUCING VULNERABILITY (from the study):
      - Higher trading volume (more traders to correct mispricing)
      - External price sources (other platforms as reference)
      - More trader engagement (comments, activity)
      - Greater liquidity depth

    TRADING IMPLICATION:
      - If you detect a price shock in a low-vulnerability market: likely informed
      - If you detect a price shock in a high-vulnerability market: possible manipulation
        => Fade the move (mean reversion trade) with ~60% expected reversion

    Returns score 0-1 where 1 = highly vulnerable to manipulation.
    """
    # Normalize factors (rough heuristic based on research findings)
    vol_score = 1 / (1 + daily_volume / 10000)  # Higher volume = less vulnerable
    trader_score = 1 / (1 + n_traders / 50)
    external_score = 0.0 if has_external_price else 0.3
    comment_score = 1 / (1 + n_comments / 20)
    depth_score = 1 / (1 + liquidity_depth / 5000)

    score = (0.25 * vol_score + 0.20 * trader_score + 0.25 * external_score +
             0.15 * comment_score + 0.15 * depth_score)

    return min(max(score, 0), 1)


# =============================================================================
# SECTION 7: LMSR (Hanson's Logarithmic Market Scoring Rule)
# =============================================================================

def lmsr_cost(quantities: List[float], b: float) -> float:
    """
    Hanson's LMSR cost function.

    C(q) = b * log(sum_i exp(q_i / b))

    Where:
      q_i = total shares of outcome i purchased so far
      b = liquidity parameter (controls market maker's max loss)

    The market maker's maximum loss is bounded by b * log(n)
    where n is the number of outcomes.

    PRICE of outcome i:
      p_i = exp(q_i / b) / sum_j exp(q_j / b)

    This is equivalent to a softmax function, ensuring:
      - All prices in (0, 1)
      - Prices sum to 1
      - Continuous liquidity (always willing to trade)

    TRADING AGAINST LMSR:
      Cost to move from q to q' = C(q') - C(q)
      If you believe p_true > p_lmsr for outcome i:
        Buy shares of i until p_lmsr = p_true (information incorporation)
        Your expected profit = integral of (p_true - p_lmsr(q)) dq

    Parameters
    ----------
    quantities : list of float
        Current quantity of shares for each outcome.
    b : float
        Liquidity parameter.
    """
    q = np.array(quantities)
    return b * np.log(np.sum(np.exp(q / b)))


def lmsr_prices(quantities: List[float], b: float) -> List[float]:
    """
    Current prices under LMSR.

    p_i = exp(q_i / b) / sum_j exp(q_j / b)
    """
    q = np.array(quantities)
    exp_q = np.exp(q / b)
    return list(exp_q / np.sum(exp_q))


def lmsr_trade_cost(
    current_quantities: List[float],
    outcome_index: int,
    shares: float,
    b: float,
) -> float:
    """
    Cost to buy `shares` of outcome `outcome_index` under LMSR.

    Cost = C(q + delta) - C(q)
    where delta is zero everywhere except outcome_index where it equals shares.
    """
    q = list(current_quantities)
    cost_before = lmsr_cost(q, b)
    q[outcome_index] += shares
    cost_after = lmsr_cost(q, b)
    return cost_after - cost_before


# =============================================================================
# SECTION 8: SUMMARY OF ACTIONABLE FINDINGS
# =============================================================================

RESEARCH_SUMMARY = """
==========================================================================
QUANTITATIVE FOUNDATIONS FOR PROFITABLE PREDICTION MARKET TRADING
==========================================================================

1. ARBITRAGE (Highest Confidence, Lowest Risk)
   - $40M extracted from Polymarket in 12 months (Apr 2024 - Apr 2025)
   - 40.9% of market conditions had arbitrage opportunities
   - Buy-all arbitrage: sum(YES prices) < $1.00 minus fees
   - Cross-platform: Kalshi vs Polymarket divergences, especially near events
   - Edge size: 2-5 cents per dollar typical, up to 55 cents in extreme cases
   - Requirements: Multi-platform access, fast execution, capital for both sides
   - Risk: Settlement rule differences across platforms

2. FAVORITE-LONGSHOT BIAS (High Confidence, Moderate Risk)
   - Longshots consistently overpriced, favorites underpriced
   - Events at 80% resolve YES ~84% of the time
   - Systematic strategy: buy high-probability outcomes, sell low-probability
   - Expected edge: 2-6 percentage points
   - Required sample: ~683 observations to detect 3% edge at 95% confidence
   - Works best in short-term markets (< 1 month to expiry)
   - Long-term markets have compression bias toward 50%

3. MEAN REVERSION (Moderate Confidence, Moderate Risk)
   - 58% of Polymarket presidential markets showed negative autocorrelation
   - Daily overreaction followed by next-day reversal
   - Strategy: Fade large daily moves by 2-4% of move size
   - Manipulation experiments show ~60% reversion from shocks
   - Works best in: low-liquidity markets, event-driven spikes
   - Detect with: variance ratio test (VR < 1 confirms mean reversion)

4. KELLY CRITERION SIZING (Mathematical Foundation)
   - Full Kelly: f* = (Q - P) / (1 + Q) where Q, P are odds ratios
   - Use quarter-Kelly (k=0.25) in practice due to:
     * Uncertainty in probability estimates
     * Correlated positions
     * Fat tails in prediction markets
   - With probability uncertainty sigma:
     f_adjusted = f_kelly * k * (1 - sigma^2 / (p*(1-p)))
   - Multiple simultaneous bets: reduce each by ~1/sqrt(N) for N bets

5. MARKET MAKING (Requires Infrastructure)
   - Polymarket CLOB enables limit order placement
   - Liquidity rewards via Q-score (spread tightness, depth, activity)
   - Risk: 40-50 point instant moves on news events
   - Requires: Low-latency infrastructure, real-time news monitoring
   - Profitable but capital-intensive

6. BAYESIAN UPDATING (Edge Detection Framework)
   - posterior_odds = prior_odds * likelihood_ratio
   - Update faster than market when new information arrives
   - Combine multiple information sources with sequential updates
   - Market price serves as consensus prior; your edge is better updating

7. RISK MANAGEMENT
   - Full Kelly: P(50% drawdown) = 50%, P(halving before doubling) = 33%
   - Quarter Kelly: P(50% drawdown) ≈ 6%, dramatically safer
   - Diversify across uncorrelated markets
   - Total portfolio risk: sum of individual Kelly fractions should not exceed
     25-50% of bankroll
   - Monitor for correlation regime changes (e.g., all politics markets
     become correlated during major events)

CONDITIONS FOR PROFITABILITY:
   - Edge must exceed transaction costs (typically 2% round-trip on Polymarket)
   - Minimum edge of 3-5% needed for consistent profitability after costs
   - 100+ trades needed to distinguish skill from luck at 95% confidence
   - Capital efficiency matters: locked capital has opportunity cost
   - Speed matters: arbitrage edges decay in seconds to minutes
   - Calibration matters: overestimating edge leads to over-betting and ruin
==========================================================================
"""


if __name__ == "__main__":
    print(RESEARCH_SUMMARY)

    # Example calculations
    print("\n--- Kelly Criterion Examples ---")

    # Example 1: You think an event is 65% likely, market says 55%
    result = kelly_binary(0.65, 0.55)
    print(f"\np_true=0.65, p_market=0.55:")
    print(f"  Full Kelly fraction: {result.fraction:.4f}")
    print(f"  Edge: {result.edge:.4f}")
    print(f"  Expected growth: {result.expected_growth:.6f}")

    # With fractional Kelly and uncertainty
    frac_result = fractional_kelly(0.65, 0.55, kelly_fraction=0.25, uncertainty_std=0.05)
    print(f"  Quarter Kelly (5% uncertainty): {frac_result.fraction:.4f}")

    # Example 2: Arbitrage detection
    print("\n--- Arbitrage Detection ---")
    arb = detect_single_market_arbitrage([0.30, 0.25, 0.35], fee_rate=0.02)
    if arb:
        print(f"  {arb.description}")
    else:
        print("  No arbitrage (prices sum to", sum([0.30, 0.25, 0.35]), ")")

    arb2 = detect_single_market_arbitrage([0.30, 0.25, 0.38], fee_rate=0.02)
    if arb2:
        print(f"  {arb2.description}")

    # Example 3: Risk of ruin
    print("\n--- Risk of Ruin ---")
    for k in [1.0, 0.5, 0.25]:
        p = max_drawdown_probability(k, 0.5)
        print(f"  Kelly fraction {k}: P(50% drawdown) = {p:.4f}")

    # Example 4: Variance ratio test
    print("\n--- Mean Reversion Detection ---")
    np.random.seed(42)
    # Simulate mean-reverting prices
    prices_mr = [0.50]
    for _ in range(200):
        shock = np.random.normal(0, 0.02)
        reversion = -0.3 * (prices_mr[-1] - 0.50)
        prices_mr.append(np.clip(prices_mr[-1] + shock + reversion, 0.01, 0.99))
    vr, z = variance_ratio_test(prices_mr, q=5)
    print(f"  Variance ratio (mean-reverting sim): VR={vr:.3f}, z={z:.3f}")
    print(f"  {'Mean reversion detected' if z < -1.96 else 'Not significant' if abs(z) < 1.96 else 'Momentum detected'}")
