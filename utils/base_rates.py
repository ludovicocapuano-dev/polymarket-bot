"""
Base Rate Database (v12.9)
===========================
Historical base rates for common prediction market event types.
The invisible number everyone ignores.

"Bots don't read op-eds. They read denominators."

Sources: historical data, academic papers, Polymarket resolution history.
75% of Polymarket markets resolve NO (platform data).
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Base Rate Tables ──────────────────────────────────────────

POLITICAL_BASE_RATES = {
    # US Elections
    "incumbent_approval_50plus_wins": 0.82,
    "incumbent_reelection": 0.70,
    "party_holds_after_two_terms": 0.38,
    "primary_frontrunner_wins": 0.75,
    "senate_incumbent_wins": 0.84,
    "house_incumbent_wins": 0.90,
    "governor_incumbent_wins": 0.78,
    # Congressional actions
    "bill_passes_after_committee": 0.45,
    "supreme_court_nominee_confirmed": 0.85,
    "government_shutdown_resolved_30d": 0.92,
    "impeachment_conviction": 0.04,
    # International
    "un_resolution_passes": 0.55,
    "trade_deal_after_talks": 0.35,
    "sanctions_imposed_after_threat": 0.60,
}

GEOPOLITICAL_BASE_RATES = {
    "ceasefire_after_talks": 0.41,
    "ceasefire_holds_30d": 0.55,
    "military_intervention_after_threat": 0.25,
    "regime_change_within_year": 0.08,
    "nato_article5_invocation": 0.02,
    "nuclear_weapon_use": 0.001,
    "territorial_concession": 0.15,
    "peace_deal_after_war": 0.30,
    "coup_attempt_succeeds": 0.40,
    "leader_ousted_within_year": 0.12,
}

ECONOMIC_BASE_RATES = {
    # Fed
    "fed_holds_rate": 0.55,
    "fed_cuts_25bp": 0.30,
    "fed_cuts_50bp": 0.08,
    "fed_hikes": 0.15,
    "fed_hold_unemp_below_4": 0.74,
    # Employment
    "nfp_beats_consensus": 0.52,
    "nfp_misses_by_50k": 0.20,
    "unemployment_rises": 0.35,
    # Inflation
    "cpi_above_3pct": 0.40,
    "cpi_below_2pct": 0.25,
    # Market
    "recession_within_year": 0.15,
    "sp500_positive_year": 0.73,
    "sp500_drops_10pct": 0.30,
}

CRYPTO_BASE_RATES = {
    "btc_ath_within_year": 0.35,
    "btc_drops_20pct_quarter": 0.40,
    "btc_above_100k": 0.25,
    "eth_above_5k": 0.20,
    "major_exchange_hack": 0.15,
    "stablecoin_depeg": 0.10,
    "sec_approves_etf": 0.45,
    "defi_tvl_doubles": 0.20,
}

WEATHER_BASE_RATES = {
    # Polymarket platform data: 75% of weather markets resolve NO
    "weather_resolves_no": 0.75,
    "exact_temp_range_2f": 0.10,  # 2°F bin
    "exact_temp_range_3f": 0.15,  # 3°F bin
    "temp_above_threshold": 0.45,
    "temp_below_threshold": 0.45,
}

SPORT_BASE_RATES = {
    "nba_favorite_wins_series": 0.72,
    "nba_home_court_advantage": 0.58,
    "nba_1seed_wins_championship": 0.25,
    "nfl_favorite_wins_superbowl": 0.55,
    "premier_league_leader_march_wins": 0.78,
    "champions_league_favorite_wins": 0.30,
    "underdog_wins_championship": 0.15,
}

ENTERTAINMENT_BASE_RATES = {
    "oscar_frontrunner_wins": 0.65,
    "box_office_sequel_beats_original": 0.35,
    "album_debuts_number_one": 0.40,
}

# Combined lookup
ALL_BASE_RATES = {
    **POLITICAL_BASE_RATES,
    **GEOPOLITICAL_BASE_RATES,
    **ECONOMIC_BASE_RATES,
    **CRYPTO_BASE_RATES,
    **WEATHER_BASE_RATES,
    **SPORT_BASE_RATES,
    **ENTERTAINMENT_BASE_RATES,
}


# ── Matching Engine ───────────────────────────────────────────

# Keywords → base rate key mapping
KEYWORD_PATTERNS = [
    # Political
    (r"incumbent.*win|reelect", "incumbent_reelection"),
    (r"confirm.*fed chair|confirm.*nominee|confirm.*justice", "supreme_court_nominee_confirmed"),
    (r"shutdown.*end|shutdown.*resolve", "government_shutdown_resolved_30d"),
    (r"impeach.*convict", "impeachment_conviction"),
    (r"senate.*win.*republican|senate.*win.*democrat", "senate_incumbent_wins"),
    # Geopolitical
    (r"ceasefire", "ceasefire_after_talks"),
    (r"regime.*change|ousted|overthrow", "regime_change_within_year"),
    (r"invade.*nato|nato.*article", "nato_article5_invocation"),
    (r"nuclear.*weapon|nuclear.*strike", "nuclear_weapon_use"),
    (r"peace.*deal|peace.*agreement", "peace_deal_after_war"),
    (r"coup", "coup_attempt_succeeds"),
    # Economic
    (r"fed.*cut|rate.*cut", "fed_cuts_25bp"),
    (r"fed.*hold|rate.*hold|no.*change.*fed", "fed_holds_rate"),
    (r"fed.*hike|rate.*hike", "fed_hikes"),
    (r"nonfarm|payroll|jobs.*added", "nfp_beats_consensus"),
    (r"unemployment.*rate", "unemployment_rises"),
    (r"cpi.*above|inflation.*above", "cpi_above_3pct"),
    (r"recession", "recession_within_year"),
    # Crypto
    (r"bitcoin.*100|btc.*100", "btc_above_100k"),
    (r"ethereum.*5000|eth.*5000", "eth_above_5k"),
    (r"etf.*approv", "sec_approves_etf"),
    # Weather
    (r"temperature|highest temp|lowest temp", "exact_temp_range_2f"),
    # Sport
    (r"nba.*finals|nba.*champion", "nba_1seed_wins_championship"),
    (r"premier.*league.*win", "premier_league_leader_march_wins"),
    (r"champions.*league.*win", "champions_league_favorite_wins"),
    (r"super.*bowl", "nfl_favorite_wins_superbowl"),
    # Entertainment
    (r"oscar|academy.*award", "oscar_frontrunner_wins"),
]


def get_base_rate(question: str) -> Optional[tuple[str, float]]:
    """
    Match a market question to its historical base rate.

    Returns: (base_rate_key, base_rate_value) or None if no match.
    """
    q = question.lower()
    for pattern, key in KEYWORD_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            rate = ALL_BASE_RATES.get(key)
            if rate is not None:
                return (key, rate)
    return None


def base_rate_edge(question: str, market_price: float) -> Optional[dict]:
    """
    Calculate edge between base rate and market price.

    Returns dict with base_rate, market_price, edge, signal, or None.
    """
    result = get_base_rate(question)
    if result is None:
        return None

    key, base_rate = result
    edge = base_rate - market_price

    if abs(edge) < 0.05:
        signal = "SKIP"
    elif edge > 0:
        signal = "BUY_YES"
    else:
        signal = "BUY_NO"

    return {
        "base_rate_key": key,
        "base_rate": base_rate,
        "market_price": market_price,
        "edge": edge,
        "abs_edge": abs(edge),
        "signal": signal,
    }
