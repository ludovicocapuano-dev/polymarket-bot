"""
Logit-Space Market Making (bs-p / Polymarket-kernel inspired)
==============================================================
Implements Avellaneda-Stoikov optimal quoting in logit space,
inspired by the bs-p (Polymarket-kernel) Rust crate.

In prediction markets, probabilities are bounded [0,1]. Working in logit
space (log-odds) gives us an unbounded, symmetric space where Gaussian
assumptions are more appropriate. This is the same transform used by
logistic regression and is the natural parameterization for binary markets.

Key functions:
  - logit(p): probability → log-odds
  - expit(x): log-odds → probability
  - optimal_quotes(): Avellaneda-Stoikov in logit space → (bid_p, ask_p)
  - implied_belief_vol(): extract volatility from observed spread

Reference:
  Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book"
  Adapted for prediction markets by operating in logit (log-odds) space.

Usage:
    from utils.logit_market_maker import optimal_quotes, implied_belief_vol

    bid_p, ask_p = optimal_quotes(
        belief=0.65,        # our probability estimate
        inventory=2.0,      # net shares held (positive = long)
        sigma=0.15,         # volatility in logit space
        gamma=0.10,         # risk aversion parameter
        tau=0.5,            # time to expiry (fraction of total)
    )
    # bid_p ~ 0.61, ask_p ~ 0.69  (skewed by inventory)
"""

import math
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# ── Core Transforms ──────────────────────────────────────────────

def logit(p: float) -> float:
    """
    Convert probability to logit (log-odds).

    logit(p) = ln(p / (1 - p))

    Clamps p to (1e-8, 1-1e-8) to avoid infinities.
    """
    p = max(1e-8, min(1.0 - 1e-8, p))
    return math.log(p / (1.0 - p))


def expit(x: float) -> float:
    """
    Convert logit (log-odds) back to probability.

    expit(x) = 1 / (1 + exp(-x))

    Numerically stable implementation.
    """
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)


# ── Optimal Quoting (Avellaneda-Stoikov in Logit Space) ──────────

def optimal_quotes(
    belief: float,
    inventory: float,
    sigma: float,
    gamma: float = 0.10,
    tau: float = 0.5,
) -> Tuple[float, float]:
    """
    Compute optimal bid and ask probabilities using Avellaneda-Stoikov
    in logit (log-odds) space.

    The market maker quotes around their belief, adjusting for:
    1. Inventory risk: skew quotes to reduce inventory
    2. Volatility: wider spread when vol is higher
    3. Time horizon: spread compresses as tau → 0

    In logit space:
      reservation_logit = belief_logit - gamma * inventory * sigma^2 * tau
      half_spread = gamma * sigma^2 * tau + ln(1 + gamma / kappa)
      bid_logit = reservation_logit - half_spread
      ask_logit = reservation_logit + half_spread

    Where kappa controls the fill rate (we set kappa = 1.5 as default,
    typical for prediction market liquidity levels).

    Args:
        belief: our probability estimate (0, 1)
        inventory: net shares held (positive = long, negative = short)
        sigma: volatility in logit space (typical: 0.10-0.30)
        gamma: risk aversion parameter (higher = wider spread)
        tau: time to expiry as fraction (1.0 = full horizon, 0.0 = expired)

    Returns:
        (bid_probability, ask_probability) — both in (0, 1)
    """
    # Default kappa (fill rate parameter) — higher = more aggressive quotes
    kappa = 1.5

    # Convert belief to logit space
    belief_logit = logit(belief)

    # Reservation price (logit): adjust for inventory risk
    # When long (inventory > 0), reservation drops (want to sell)
    # When short (inventory < 0), reservation rises (want to buy)
    reservation_logit = belief_logit - gamma * inventory * (sigma ** 2) * tau

    # Optimal half-spread in logit space
    # Two components: (1) inventory risk premium, (2) adverse selection / fill rate
    half_spread = gamma * (sigma ** 2) * tau + math.log(1.0 + gamma / kappa)

    # Ensure minimum spread (at least 1 tick in probability ~ 0.01)
    min_half_spread_logit = 0.04  # ~1 cent at p=0.50
    half_spread = max(half_spread, min_half_spread_logit)

    # Compute bid/ask in logit space
    bid_logit = reservation_logit - half_spread
    ask_logit = reservation_logit + half_spread

    # Convert back to probability space
    bid_p = expit(bid_logit)
    ask_p = expit(ask_logit)

    # Clamp to valid Polymarket price range [0.01, 0.99]
    bid_p = max(0.01, min(0.99, bid_p))
    ask_p = max(0.01, min(0.99, ask_p))

    # Ensure bid < ask (can be violated with extreme inventory)
    if bid_p >= ask_p:
        mid = (bid_p + ask_p) / 2.0
        bid_p = max(0.01, mid - 0.01)
        ask_p = min(0.99, mid + 0.01)

    return (round(bid_p, 4), round(ask_p, 4))


def optimal_spread(
    sigma: float,
    gamma: float = 0.10,
    tau: float = 0.5,
    kappa: float = 1.5,
) -> float:
    """
    Compute optimal full spread in logit space.

    Useful for sizing decisions — wider spread = more uncertainty.

    Returns:
        Full spread in logit units.
    """
    half = gamma * (sigma ** 2) * tau + math.log(1.0 + gamma / kappa)
    return 2.0 * half


# ── Implied Volatility from Observed Spread ──────────────────────

def implied_belief_vol(
    bid_p: float,
    ask_p: float,
    gamma: float = 0.10,
    tau: float = 0.5,
    kappa: float = 1.5,
) -> Optional[float]:
    """
    Extract implied volatility (in logit space) from an observed bid-ask spread.

    Given observed bid/ask prices, we invert the A-S spread formula:
      spread_logit = 2 * [gamma * sigma^2 * tau + ln(1 + gamma/kappa)]
      sigma = sqrt((spread_logit/2 - ln(1 + gamma/kappa)) / (gamma * tau))

    Args:
        bid_p: observed bid probability
        ask_p: observed ask probability
        gamma: assumed risk aversion
        tau: assumed time to expiry fraction
        kappa: assumed fill rate parameter

    Returns:
        Implied sigma in logit space, or None if calculation fails.
    """
    if bid_p >= ask_p or bid_p <= 0 or ask_p >= 1:
        return None

    bid_logit = logit(bid_p)
    ask_logit = logit(ask_p)
    spread_logit = ask_logit - bid_logit

    half_spread = spread_logit / 2.0
    fill_component = math.log(1.0 + gamma / kappa)

    vol_sq_component = half_spread - fill_component
    if vol_sq_component <= 0 or gamma * tau <= 0:
        return None

    sigma_sq = vol_sq_component / (gamma * tau)
    if sigma_sq < 0:
        return None

    return math.sqrt(sigma_sq)


# ── Inventory Skew ───────────────────────────────────────────────

def inventory_skew_cents(
    belief: float,
    inventory: float,
    sigma: float = 0.15,
    gamma: float = 0.10,
    tau: float = 0.5,
) -> float:
    """
    Compute the mid-price skew (in cents) caused by inventory.

    Positive = mid shifted down (want to sell), negative = shifted up (want to buy).

    Useful for adjusting the reservation price of existing positions.

    Returns:
        Skew in cents (e.g., 2.5 means mid is shifted down by $0.025)
    """
    # Skew in logit space
    skew_logit = gamma * inventory * (sigma ** 2) * tau

    # Convert to probability skew at the belief point
    # d(expit)/d(logit) = expit * (1 - expit)
    p = max(0.01, min(0.99, belief))
    prob_sensitivity = p * (1.0 - p)
    skew_prob = skew_logit * prob_sensitivity

    return round(skew_prob * 100, 2)  # in cents


# ── Two-Sided Quote Generator ───────────────────────────────────

def two_sided_quotes(
    belief: float,
    inventory: float,
    sigma: float,
    gamma: float = 0.10,
    tau: float = 0.5,
    size: float = 10.0,
    tick: float = 0.01,
) -> dict:
    """
    Generate a complete two-sided quote ready for order placement.

    Returns a dict with bid/ask prices, sizes, and metadata.
    Sizes are adjusted for inventory: larger on the reducing side.

    Args:
        belief: our probability estimate
        inventory: net shares (positive = long)
        sigma: logit-space volatility
        gamma: risk aversion
        tau: time to expiry fraction
        size: base dollar size per side
        tick: price tick (Polymarket = $0.01)

    Returns:
        dict with bid_price, ask_price, bid_size, ask_size, spread, skew
    """
    bid_p, ask_p = optimal_quotes(belief, inventory, sigma, gamma, tau)

    # Round to tick
    bid_p = max(0.01, math.floor(bid_p / tick) * tick)
    ask_p = min(0.99, math.ceil(ask_p / tick) * tick)

    # Adjust sizes for inventory reduction
    # If long (inventory > 0), increase ask size (want to sell more)
    # If short (inventory < 0), increase bid size (want to buy more)
    inv_adj = min(abs(inventory) * 0.1, 0.5)  # max 50% size adjustment
    if inventory > 0:
        bid_size = size * (1.0 - inv_adj)
        ask_size = size * (1.0 + inv_adj)
    else:
        bid_size = size * (1.0 + inv_adj)
        ask_size = size * (1.0 - inv_adj)

    spread = ask_p - bid_p
    skew = inventory_skew_cents(belief, inventory, sigma, gamma, tau)

    return {
        "bid_price": round(bid_p, 2),
        "ask_price": round(ask_p, 2),
        "bid_size": round(bid_size, 2),
        "ask_size": round(ask_size, 2),
        "spread": round(spread, 4),
        "spread_cents": round(spread * 100, 1),
        "skew_cents": skew,
        "mid_price": round((bid_p + ask_p) / 2, 4),
        "reservation_price": round(expit(logit(belief) - gamma * inventory * sigma**2 * tau), 4),
        "implied_vol": sigma,
    }
