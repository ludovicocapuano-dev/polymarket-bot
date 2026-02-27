"""
Avellaneda-Stoikov Optimal Execution for Prediction Markets.

Adapted for binary outcome markets [0,1]:
- σ²τ = p(1-p)  (natural variance of binary outcome)
- Two separate γ: GAMMA_INVENTORY (inventory skew) and GAMMA_SPREAD (half-spread)
- γ scaled by 24h volume (proxy for κ/arrival rate)
- VPIN premium for adverse selection (complements VPIN>=0.7 gate in validator)

Reference: Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book"
"""

import logging

logger = logging.getLogger(__name__)

# --- Costanti ---
GAMMA_INVENTORY = 0.30   # Inventory risk aversion (shift reservation price)
GAMMA_SPREAD = 0.05      # Spread risk aversion (widen half-spread)
VOL_HIGH = 10_000        # Volume 24h soglia liquido (γ × 0.7)
VOL_LOW = 1_000          # Volume 24h soglia illiquido (γ × 1.5)
VPIN_PREMIUM_MAX = 0.02  # Max premium per adverse selection (2¢)
TICK = 0.01              # Minimum price increment


def binary_variance(price: float) -> float:
    """σ²τ per binary outcome: p(1-p). Max a p=0.5, zero a 0 e 1."""
    p = max(0.0, min(1.0, price))
    return p * (1.0 - p)


def gamma_effective(gamma_base: float, volume_24h: float) -> float:
    """
    Scala γ per liquidità (proxy di κ arrival rate).
    Liquido (vol >= VOL_HIGH) → γ × 0.7  (meno conservativo)
    Illiquido (vol <= VOL_LOW) → γ × 1.5  (più conservativo)
    Intermedio → interpolazione lineare
    """
    if volume_24h >= VOL_HIGH:
        return gamma_base * 0.7
    if volume_24h <= VOL_LOW:
        return gamma_base * 1.5
    # Interpolazione lineare tra 1.5 e 0.7
    ratio = (volume_24h - VOL_LOW) / (VOL_HIGH - VOL_LOW)
    scale = 1.5 - ratio * (1.5 - 0.7)
    return gamma_base * scale


def reservation_price(mid: float, inventory_frac: float, gamma: float,
                      sigma_sq_tau: float) -> float:
    """
    Reservation price: r = s − q × γ × σ²τ
    Shift down per inventario (più inventario → bid più basso).
    """
    return mid - inventory_frac * gamma * sigma_sq_tau


def optimal_half_spread(gamma: float, sigma_sq_tau: float,
                        vpin: float = 0.0) -> float:
    """
    Optimal half-spread: δ/2 = γ × σ²τ / 2 + vpin_premium
    VPIN premium: 0-2¢ proporzionale a VPIN (0 = no toxicity, 0.7+ = max).
    """
    base = gamma * sigma_sq_tau / 2.0
    # VPIN premium: lineare 0→0 a 0.7→VPIN_PREMIUM_MAX
    vpin_premium = 0.0
    if vpin > 0.0:
        vpin_premium = min(vpin / 0.7, 1.0) * VPIN_PREMIUM_MAX
    return base + vpin_premium


def market_inventory_frac(open_trades: list, market_id: str,
                          budget: float) -> float:
    """
    Calcola q = esposizione su questo mercato / budget strategia.
    Ritorna 0.0 se nessuna esposizione, clippato a [0, 1].
    """
    if budget <= 0:
        return 0.0
    exposure = sum(t.size for t in open_trades if t.market_id == market_id)
    return min(exposure / budget, 1.0)


def optimal_bid(mid: float, best_bid: float, best_ask: float,
                target: float, inventory_frac: float = 0.0,
                volume_24h: float = 0.0, vpin: float = 0.0) -> float:
    """
    Calcola il bid ottimale A-S per prediction markets.

    bid = r − δ/2, clippato a [best_bid, min(mid, target)]

    Ritorna il prezzo bid arrotondato a 2 decimali.
    """
    sigma_sq_tau = binary_variance(mid)

    gamma_inv = gamma_effective(GAMMA_INVENTORY, volume_24h)
    gamma_spr = gamma_effective(GAMMA_SPREAD, volume_24h)

    r = reservation_price(mid, inventory_frac, gamma_inv, sigma_sq_tau)
    half_spread = optimal_half_spread(gamma_spr, sigma_sq_tau, vpin)

    bid = r - half_spread

    # Clip: mai sotto best_bid, mai sopra min(mid, target)
    ceiling = min(mid, target)
    bid = max(bid, best_bid)
    bid = min(bid, ceiling)

    bid = round(bid, 2)

    # Log A-S calculation
    naive = round(min(best_bid + TICK, target), 2)
    delta = bid - naive
    logger.info(
        f"[AS] mid={mid:.3f} inv={inventory_frac:.2f} vol=${volume_24h:,.0f} "
        f"vpin={vpin:.2f} σ²τ={sigma_sq_tau:.4f} → "
        f"r={r:.4f} δ/2={half_spread:.4f} bid={bid:.2f} "
        f"naive={naive:.2f} Δ={delta:+.3f}"
    )

    return bid
