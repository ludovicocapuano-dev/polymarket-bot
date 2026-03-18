"""
KL-Divergence Arbitrage Scanner (v12.9)
========================================
Finds correlated markets that should be priced similarly but aren't.
Profit from convergence — not from predicting who wins.

D_KL(P‖Q) = Σ Pᵢ · ln(Pᵢ / Qᵢ)

Example: "X wins nomination" at 70%, "X wins general" at 55%.
History says general should be ~62% given nomination at 70%.
KL flags the gap → buy underpriced, hedge with the other.
"""

import logging
import re
from itertools import combinations
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Minimum KL divergence to flag as arbitrage opportunity
KL_THRESHOLD = 0.10


def kl_divergence(p: list[float], q: list[float]) -> float:
    """
    KL divergence D_KL(P‖Q).
    Measures how much P diverges from Q.
    """
    p = np.array(p, dtype=float)
    q = np.array(q, dtype=float)
    # Avoid log(0) — clip to small positive values
    p = np.clip(p, 1e-8, 1.0 - 1e-8)
    q = np.clip(q, 1e-8, 1.0 - 1e-8)
    # Normalize
    p = p / p.sum()
    q = q / q.sum()
    return float(np.sum(p * np.log(p / q)))


def symmetric_kl(p: list[float], q: list[float]) -> float:
    """Symmetric KL: average of D_KL(P‖Q) and D_KL(Q‖P)."""
    return (kl_divergence(p, q) + kl_divergence(q, p)) / 2


# ── Correlation patterns for Polymarket ───────────────────────

# Markets that should be logically correlated
CORRELATION_PATTERNS = [
    # Politics: nomination → general (general should be <= nomination)
    (r"win.*nomination|win.*primary", r"win.*general|win.*president"),
    # Politics: party wins state → party wins overall
    (r"win.*\b(OH|PA|MI|WI|AZ|GA|NV)\b", r"win.*president|win.*election"),
    # Economics: rate cut → recession (correlated)
    (r"fed.*cut|rate.*cut", r"recession"),
    # Crypto: BTC target → ETH target (correlated movement)
    (r"bitcoin.*above|btc.*above", r"ethereum.*above|eth.*above"),
    # Sport: win conference → win championship
    (r"win.*conference|win.*division", r"win.*championship|win.*finals"),
    # Geopolitics: talks → ceasefire → peace deal (chain)
    (r"talks|negotiate", r"ceasefire"),
    (r"ceasefire", r"peace.*deal|peace.*agreement"),
]


def find_correlated_markets(markets: list[dict]) -> list[tuple[dict, dict, str]]:
    """
    Find pairs of markets that should be logically correlated.

    Returns list of (market_a, market_b, relationship) tuples.
    """
    pairs = []
    for i, a in enumerate(markets):
        q_a = (a.get("question") or "").lower()
        for j, b in enumerate(markets):
            if i >= j:
                continue
            q_b = (b.get("question") or "").lower()

            for pat_a, pat_b in CORRELATION_PATTERNS:
                if re.search(pat_a, q_a) and re.search(pat_b, q_b):
                    pairs.append((a, b, f"{pat_a} → {pat_b}"))
                elif re.search(pat_b, q_a) and re.search(pat_a, q_b):
                    pairs.append((b, a, f"{pat_a} → {pat_b}"))

    return pairs


def scan_kl_arbitrage(markets: list[dict],
                      threshold: float = KL_THRESHOLD) -> list[dict]:
    """
    Scan for KL-divergence arbitrage opportunities.

    markets: list of dicts with 'question', 'outcomePrices' (or 'yes_price')

    Returns list of opportunities with KL score and suggested trades.
    """
    # Build price distributions
    priced_markets = []
    for m in markets:
        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            import json
            try:
                prices = json.loads(prices)
            except Exception:
                continue

        if prices and len(prices) >= 2:
            yes_p = float(prices[0])
            no_p = float(prices[1])
            if 0.01 < yes_p < 0.99:
                priced_markets.append({
                    **m,
                    "dist": [yes_p, no_p],
                    "yes_price": yes_p,
                })

    # Find correlated pairs
    correlated = find_correlated_markets(priced_markets)

    opportunities = []
    for a, b, relationship in correlated:
        kl = symmetric_kl(a["dist"], b["dist"])
        if kl >= threshold:
            q_a = (a.get("question") or "")[:50]
            q_b = (b.get("question") or "")[:50]

            # Determine which is overpriced
            # In a chain (nomination → general), general should be <= nomination
            if a["yes_price"] < b["yes_price"]:
                trade = f"BUY '{q_a}' / SELL '{q_b}'"
            else:
                trade = f"BUY '{q_b}' / SELL '{q_a}'"

            opp = {
                "market_a": a.get("question", ""),
                "market_b": b.get("question", ""),
                "price_a": a["yes_price"],
                "price_b": b["yes_price"],
                "kl_divergence": round(kl, 4),
                "relationship": relationship,
                "suggested_trade": trade,
                "price_gap": abs(a["yes_price"] - b["yes_price"]),
            }
            opportunities.append(opp)

            logger.info(
                f"[KL-ARB] KL={kl:.4f} gap={abs(a['yes_price']-b['yes_price']):.2f} | "
                f"{q_a} ({a['yes_price']:.2f}) vs {q_b} ({b['yes_price']:.2f})"
            )

    # Sort by KL divergence (highest first)
    opportunities.sort(key=lambda x: x["kl_divergence"], reverse=True)

    if opportunities:
        logger.info(f"[KL-ARB] Found {len(opportunities)} divergence opportunities")

    return opportunities
