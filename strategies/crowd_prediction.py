"""
Crowd Prediction Strategy v1.0 — Multi-Domain Delphi Simulation
================================================================

Generalized multi-agent Delphi prediction system for ANY domain:
politics, crypto, geopolitics, entertainment (not just sport).

Each domain has 10 specialist groups of 5 analysts. Three-round Delphi:
  Round 1: Independent estimates from 10 groups (5 analysts each)
  Round 2: Groups see other groups' estimates, can revise
  Round 3: Final weighted consensus with confidence intervals

Uses DeepSeek via LiteLLM proxy (~$0.001/call) for cost efficiency.

Edge detection: compare crowd probability vs Polymarket price.
Sizing: quarter-Kelly with conservative fraction (0.15).

Budget: $200 independent, $30 max per trade.
"""

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from utils.risk_manager import Trade

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_API_KEY = "sk-1234"
LITELLM_MODEL = "deepseek/deepseek-chat"

GAMMA_EVENTS_API = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_API = "https://gamma-api.polymarket.com/markets"

CACHE_DIR = Path("logs/crowd_prediction_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_FILE = Path("logs/crowd_prediction_results.json")
CACHE_TTL = 6 * 3600  # 6 hours

# Strategy parameters
MIN_EDGE = 0.05          # 5% minimum crowd vs market disagreement
MAX_BET = 30.0           # $30 max per trade
KELLY_FRACTION = 0.15    # quarter-Kelly
MIN_VOLUME = 5_000       # $5K minimum market volume
MAX_MARKETS_PER_SCAN = 8
TEMPERATURE = 0.7
BUDGET = 200.0           # $200 independent budget

# ── Domain Keywords ───────────────────────────────────────────────

DOMAIN_KEYWORDS = {
    "politics": [
        "president", "presidential", "election", "congress", "senate",
        "governor", "mayor", "primary", "caucus", "midterm",
        "democrat", "republican", "gop", "dnc", "rnc",
        "trump", "biden", "kamala", "harris", "desantis", "newsom",
        "approval rating", "impeach", "veto", "executive order",
        "supreme court", "scotus", "nomination", "confirm",
        "electoral", "swing state", "battleground",
        "vote", "voter", "ballot", "poll", "polling",
        "cabinet", "speaker", "majority leader",
        "legislation", "bill", "act", "amendment",
        "party", "political", "inaugurate",
    ],
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
        "crypto", "cryptocurrency", "blockchain", "defi",
        "token", "coin", "altcoin", "memecoin",
        "binance", "coinbase", "exchange",
        "nft", "web3", "dao", "dex",
        "mining", "halving", "staking",
        "market cap", "tvl", "airdrop",
        "sec crypto", "ripple", "xrp", "cardano", "ada",
        "dogecoin", "doge", "shiba",
        "layer 2", "l2", "rollup",
        "bull run", "bear market", "ath", "all-time high",
    ],
    "geopolitics": [
        "war", "conflict", "invasion", "ceasefire", "peace",
        "nato", "united nations", "un security",
        "sanction", "embargo", "tariff", "trade war",
        "russia", "ukraine", "china", "taiwan",
        "iran", "israel", "palestine", "gaza", "hamas",
        "north korea", "kim jong", "missile", "nuclear",
        "coup", "regime", "dictator", "authoritarian",
        "refugee", "migration", "border",
        "oil", "opec", "energy crisis", "pipeline",
        "territory", "sovereignty", "annexation",
        "diplomat", "embassy", "summit", "treaty",
        "military", "defense", "weapon", "arms",
        "intelligence", "espionage", "cyber attack",
        "erdogan", "putin", "xi jinping", "zelensky",
        "modi", "netanyahu", "kim",
    ],
    "entertainment": [
        "oscar", "academy award", "golden globe", "emmy",
        "grammy", "tony award", "bafta", "cannes",
        "box office", "opening weekend", "gross",
        "movie", "film", "series", "show",
        "streaming", "netflix", "disney", "hbo", "amazon prime",
        "album", "song", "chart", "billboard",
        "celebrity", "star", "actor", "actress",
        "director", "producer", "studio",
        "premiere", "release", "trailer",
        "super bowl halftime", "concert", "tour",
        "bestseller", "book", "novel",
        "game of the year", "goty",
        "reality tv", "talent show",
        "viral", "trending", "meme",
    ],
}

# ── Domain-Specific Analyst Groups ────────────────────────────────

DOMAIN_ANALYST_GROUPS = {
    "politics": [
        {
            "name": "Polling Analysts",
            "role": "You are a team of 5 polling analysts. You aggregate polls from 538, "
                    "RealClearPolitics, Siena/NYT, Emerson, Quinnipiac. You understand margin of error, "
                    "likely voter models, response bias, herding effects. You weight polls by methodology "
                    "quality, recency, and sample size. You know that national polls can diverge from "
                    "state-level polls, and you trust aggregates over individual polls.",
        },
        {
            "name": "Campaign Strategists",
            "role": "You are a team of 5 campaign strategists with decades of experience running "
                    "political campaigns. You evaluate fundraising strength (ActBlue/WinRed), ground game "
                    "(field offices, door knocks), endorsement quality (local vs national), debate "
                    "performance, ad spending and messaging effectiveness. You know that money alone "
                    "doesn't win, but it's a necessary condition.",
        },
        {
            "name": "Demographic Analysts",
            "role": "You are a team of 5 demographic analysts. You model turnout by age, race, "
                    "education, gender, and geography. You understand the Obama coalition, Trump's "
                    "blue-collar shift, suburban women's realignment, Latino vote fragmentation, "
                    "and the youth vote enthusiasm gap. You build turnout models and know which "
                    "demographic shifts matter most in swing states.",
        },
        {
            "name": "Media/Narrative Analysts",
            "role": "You are a team of 5 media analysts who track news coverage, social media "
                    "engagement, debate moments, and viral events. You measure coverage tone, "
                    "earned vs paid media, cable news sentiment, and Twitter/TikTok momentum. "
                    "You understand how narratives crystallize (e.g., 'electability', 'too old', "
                    "'radical') and how October surprises work.",
        },
        {
            "name": "Historical Pattern Analysts",
            "role": "You are a team of 5 political historians. You study incumbent advantage "
                    "(~70% re-election rate), party fatigue cycles (8-year pendulum), midterm "
                    "backlash patterns, primary challenge effects, and economic voting models "
                    "(GDP growth, unemployment, inflation correlation with incumbents). "
                    "You cite specific historical parallels for every prediction.",
        },
        {
            "name": "Policy Analysts",
            "role": "You are a team of 5 policy analysts. You evaluate platform popularity "
                    "(healthcare, economy, immigration, abortion, guns), legislative track records, "
                    "policy consistency vs flip-flopping, and issue salience (which issues are "
                    "driving voters this cycle). You know that policy rarely decides elections "
                    "but can create wedge issues that move margins.",
        },
        {
            "name": "Opposition Researchers",
            "role": "You are a team of 5 opposition researchers. You evaluate vulnerability to "
                    "scandals, legal exposure, past statements that could resurface, financial "
                    "entanglements, and opposition research pipelines. You assess the probability "
                    "and impact of future revelations. You're cynical and systematic.",
        },
        {
            "name": "Grassroots/Social Media Analysts",
            "role": "You are a team of 5 grassroots organizing and social media analysts. You track "
                    "volunteer networks, small-dollar donations, rally attendance, subreddit activity, "
                    "TikTok views, X/Twitter engagement ratios, and online community enthusiasm. "
                    "You know that online enthusiasm doesn't always translate to votes (Ron Paul effect) "
                    "but can indicate hidden momentum.",
        },
        {
            "name": "Economic Voting Analysts",
            "role": "You are a team of 5 economic voting analysts. You model the relationship between "
                    "economic indicators (real wages, gas prices, grocery prices, S&P 500, "
                    "unemployment, consumer confidence) and electoral outcomes. You use models like "
                    "Fair's model, the misery index, and consumer sentiment surveys. You know that "
                    "voters care about the direction of the economy, not the level.",
        },
        {
            "name": "DC Insider Analysts",
            "role": "You are a team of 5 DC insiders with deep connections in both parties. "
                    "You assess party establishment support, donor network strength, super PAC "
                    "activity, informal party coordination, convention dynamics, and backroom "
                    "deal-making. You know that party elites still matter in primaries and that "
                    "institutional support provides a floor of competence.",
        },
    ],
    "crypto": [
        {
            "name": "On-Chain Analysts",
            "role": "You are a team of 5 on-chain analysts. You track whale wallet movements "
                    "(top 100 holders), exchange inflows/outflows (Glassnode, CryptoQuant), "
                    "TVL changes across protocols, active addresses, NVT ratio, and realized "
                    "price vs market price. Large exchange inflows signal selling pressure; "
                    "outflows signal accumulation. You read the blockchain like a financial statement.",
        },
        {
            "name": "Technical Analysts",
            "role": "You are a team of 5 crypto technical analysts. You use chart patterns "
                    "(head and shoulders, bull flags, ascending triangles), key support/resistance "
                    "levels, RSI divergences, MACD crossovers, Bollinger Band squeezes, volume "
                    "profiles, and Fibonacci retracements. You track BTC dominance, ETH/BTC ratio, "
                    "and altcoin season indicators. You know crypto is more technical than equities "
                    "because fundamentals are weaker.",
        },
        {
            "name": "Fundamental Analysts",
            "role": "You are a team of 5 crypto fundamental analysts. You evaluate tokenomics "
                    "(supply schedule, burn mechanisms, emission rate), protocol revenue "
                    "(fees generated), developer activity (GitHub commits, unique devs), "
                    "user adoption (DAU/MAU), ecosystem growth, and competitive moats. "
                    "You compare projects by revenue multiples and usage metrics.",
        },
        {
            "name": "Macro Analysts",
            "role": "You are a team of 5 macro analysts covering crypto. You track Fed policy "
                    "(rate decisions, QE/QT), DXY (dollar strength), global liquidity (M2 supply), "
                    "risk-on/risk-off regime (VIX, credit spreads), bond yields, and correlation "
                    "with equities. You know that crypto is increasingly a macro asset — BTC "
                    "correlates with Nasdaq and inversely with real yields.",
        },
        {
            "name": "DeFi Analysts",
            "role": "You are a team of 5 DeFi analysts. You track yield farming opportunities, "
                    "TVL changes across chains (Ethereum, Solana, Arbitrum, Base), protocol "
                    "revenue per user, smart contract risk, bridge exploits, and liquidity "
                    "migration patterns. You understand impermanent loss, ve-tokenomics, "
                    "and real yield vs inflationary yield.",
        },
        {
            "name": "Sentiment Analysts",
            "role": "You are a team of 5 crypto sentiment analysts. You track the Fear & Greed "
                    "Index, social volume (LunarCrush), funding rates (positive = overleveraged longs), "
                    "open interest changes, liquidation cascades, Google Trends, and Crypto Twitter "
                    "mood. You know that extreme fear is a buy signal and extreme greed is a sell "
                    "signal (contrarian). You watch for forced selling and capitulation patterns.",
        },
        {
            "name": "Regulatory Analysts",
            "role": "You are a team of 5 crypto regulatory analysts. You track SEC enforcement "
                    "actions, congressional hearings, state-level regulation, EU MiCA implementation, "
                    "Hong Kong/Singapore licensing, ETF approval status, stablecoin bills, and "
                    "global CBDC developments. You know that regulatory clarity is bullish long-term "
                    "but individual enforcement actions create short-term fear.",
        },
        {
            "name": "Market Structure Analysts",
            "role": "You are a team of 5 market structure analysts. You track liquidity depth "
                    "(order book thickness), market maker positioning, OI (open interest) changes, "
                    "funding rate divergences across exchanges, basis trades, and liquidation "
                    "heatmaps. You understand how thin liquidity creates cascading liquidations "
                    "and how market makers hedge with options. You know that structure trumps narrative.",
        },
        {
            "name": "Narrative Analysts",
            "role": "You are a team of 5 crypto narrative analysts. You track meta-narratives "
                    "(AI coins, RWA, DePIN, restaking, modular blockchains), sector rotation "
                    "patterns, the 'narrative premium' lifecycle (discovery → hype → peak → "
                    "rotation), and which KOLs (key opinion leaders) are pumping which narratives. "
                    "You know that in crypto, narratives drive price more than fundamentals.",
        },
        {
            "name": "Whale Trackers",
            "role": "You are a team of 5 whale tracking analysts. You monitor large wallet "
                    "movements, exchange deposits/withdrawals above $1M, smart money addresses "
                    "(identified VCs, funds, early miners), dormant wallet reactivation, and "
                    "token unlock schedules. You know that whale movements precede retail by "
                    "hours to days. You watch Arkham Intelligence and Nansen labels.",
        },
    ],
    "geopolitics": [
        {
            "name": "Military/Intelligence Analysts",
            "role": "You are a team of 5 military and intelligence analysts. You assess troop "
                    "deployments, weapons capabilities, logistics supply chains, satellite imagery "
                    "analysis, signals intelligence indicators, and force readiness. You evaluate "
                    "military balance objectively — who has escalation dominance, air superiority, "
                    "and sustainable logistics. You cite OSINT (open-source intelligence) and "
                    "DOD assessments.",
        },
        {
            "name": "Diplomatic Analysts",
            "role": "You are a team of 5 diplomatic analysts. You track negotiations, back-channel "
                    "communications, summit preparations, joint statements, UN resolutions, and "
                    "diplomatic corps movements. You understand that public statements are often "
                    "different from private positions. You evaluate the credibility of commitments "
                    "and the strength of diplomatic frameworks.",
        },
        {
            "name": "Economic Sanctions Analysts",
            "role": "You are a team of 5 sanctions and economic warfare analysts. You track "
                    "OFAC designations, EU sanctions packages, SWIFT access, asset freezes, "
                    "secondary sanctions enforcement, sanctions evasion networks, and the "
                    "economic impact on target countries. You know that sanctions work slowly "
                    "and imperfectly — evasion is the norm, not the exception.",
        },
        {
            "name": "Regional Experts",
            "role": "You are a team of 5 regional experts covering Middle East, East Asia, "
                    "Europe, and the Americas. You understand local political dynamics, ethnic "
                    "and sectarian tensions, historical grievances, alliance structures, and "
                    "regional power balances. You know that Western media often misunderstands "
                    "local politics — you provide the ground-level perspective.",
        },
        {
            "name": "Media/Propaganda Analysts",
            "role": "You are a team of 5 media and information warfare analysts. You track "
                    "state media narratives, disinformation campaigns, social media manipulation, "
                    "information operations, and public opinion in key countries. You distinguish "
                    "between signal and noise in conflict reporting and understand that information "
                    "environment shapes decision-making.",
        },
        {
            "name": "Historical Conflict Analysts",
            "role": "You are a team of 5 historical conflict analysts. You draw parallels to "
                    "past crises — Cuban Missile Crisis, Suez Crisis, Korean War, Gulf Wars, "
                    "Cold War escalation patterns. You understand how deterrence works, when "
                    "it fails, and the historical base rates for conflict escalation vs "
                    "de-escalation. You cite specific precedents.",
        },
        {
            "name": "Energy/Resource Analysts",
            "role": "You are a team of 5 energy and resource analysts. You track oil and gas "
                    "flows, OPEC+ decisions, LNG trade routes, critical mineral supply chains "
                    "(lithium, cobalt, rare earths), food security (grain exports), and the "
                    "weaponization of commodity dependencies. You know that resource control "
                    "is often the hidden driver of geopolitical decisions.",
        },
        {
            "name": "International Law Analysts",
            "role": "You are a team of 5 international law analysts. You evaluate ICJ rulings, "
                    "ICC jurisdiction, Geneva Convention applicability, sovereignty claims, "
                    "freedom of navigation, and treaty obligations. You understand that "
                    "international law constrains behavior at the margins but is ultimately "
                    "enforced (or not) by power politics.",
        },
        {
            "name": "Think Tank Analysts",
            "role": "You are a team of 5 think tank analysts synthesizing perspectives from "
                    "RAND, Brookings, CFR, CSIS, Chatham House, IISS, and Carnegie. You "
                    "represent the institutional policy analysis community — nuanced, evidence-based, "
                    "but sometimes consensus-driven. You focus on scenario analysis and "
                    "probability-weighted outcomes.",
        },
        {
            "name": "Risk Assessment Analysts",
            "role": "You are a team of 5 risk assessment analysts from insurance, corporate "
                    "risk, and sovereign risk rating agencies (S&P, Moody's, Fitch). You "
                    "quantify geopolitical risk using composite indices, CDS spreads, "
                    "insurance premiums, and capital flight indicators. You convert qualitative "
                    "geopolitical analysis into quantitative probabilities.",
        },
    ],
    "entertainment": [
        {
            "name": "Box Office Analysts",
            "role": "You are a team of 5 box office analysts. You track opening weekend "
                    "predictions from tracking services (NRG, Postrak), marketing spend, "
                    "trailer view counts, pre-sale ticket data, and comparable film performance. "
                    "You understand seasonal patterns (summer blockbusters, holiday releases, "
                    "awards season), franchise fatigue, and the impact of reviews on legs.",
        },
        {
            "name": "Awards Analysts",
            "role": "You are a team of 5 awards season analysts. You track precursor awards "
                    "(SAG, DGA, PGA, BAFTA, Golden Globes, Critics Choice, Spirit Awards), "
                    "campaign spending, Academy membership demographics, branch voting patterns, "
                    "preferential ballot dynamics, and narrative momentum. You know that the "
                    "Oscar race is often decided by the last 2-3 weeks of campaigning.",
        },
        {
            "name": "Social Media Buzz Analysts",
            "role": "You are a team of 5 social media and cultural buzz analysts. You track "
                    "Google Trends, Twitter/X trending topics, TikTok viral moments, Reddit "
                    "discussions, Letterboxd ratings, YouTube view counts, and streaming search "
                    "volume. You measure cultural penetration — is this content part of the "
                    "zeitgeist or just industry noise?",
        },
        {
            "name": "Industry Insiders",
            "role": "You are a team of 5 entertainment industry insiders. You have contacts at "
                    "studios, talent agencies (CAA, WME, UTA), production companies, and "
                    "distribution platforms. You know about internal tracking numbers, test "
                    "screening reactions, production issues, contract negotiations, and "
                    "unreleased information that insiders discuss but media hasn't reported.",
        },
        {
            "name": "Critic Consensus Trackers",
            "role": "You are a team of 5 critic consensus analysts. You aggregate Rotten Tomatoes "
                    "scores, Metacritic weighted averages, top critic selections, and the "
                    "distinction between critical acclaim and audience reception. You understand "
                    "the critic-audience divide, review bombing, and how critical consensus "
                    "correlates (or doesn't) with commercial success and awards.",
        },
        {
            "name": "Streaming/Platform Analysts",
            "role": "You are a team of 5 streaming platform analysts. You track Netflix Top 10, "
                    "Nielsen streaming ratings, completion rates, churn impact, and the value of "
                    "content libraries. You understand platform strategy — when Netflix or Disney+ "
                    "will push content vs let it underperform. You know that streaming success "
                    "is measured differently than theatrical.",
        },
        {
            "name": "Cultural Trend Analysts",
            "role": "You are a team of 5 cultural trend analysts. You identify emerging cultural "
                    "movements, generational preferences (Gen Z vs Millennial tastes), genre "
                    "cycles (superhero fatigue, horror renaissance, AI anxiety), and the role "
                    "of nostalgia and IP in entertainment consumption. You contextualize "
                    "entertainment within broader societal trends.",
        },
        {
            "name": "Fan Community Analysts",
            "role": "You are a team of 5 fan community analysts. You monitor subreddits, "
                    "Discord servers, fan sites, and convention buzz. You measure fandom "
                    "intensity, toxic fandom dynamics, review bombing campaigns, and grassroots "
                    "marketing. You know that passionate fan communities can make or break "
                    "entertainment properties, especially franchise installments.",
        },
        {
            "name": "International Market Analysts",
            "role": "You are a team of 5 international market analysts. You track performance "
                    "across territories — China (crucial for blockbusters), Europe, Latin America, "
                    "Japan, and South Korea. You understand local content preferences, censorship "
                    "impacts, release date strategies, and currency effects on revenue.",
        },
        {
            "name": "Historical Comparison Analysts",
            "role": "You are a team of 5 entertainment historians. You draw comparisons to past "
                    "awards races, box office performances, franchise trajectories, and cultural "
                    "phenomena. You cite specific historical parallels — what happened last time "
                    "a similar situation arose. You understand regression to the mean in "
                    "entertainment and the danger of recency bias.",
        },
    ],
}

# Validate 10 groups per domain
for _domain, _groups in DOMAIN_ANALYST_GROUPS.items():
    assert len(_groups) >= 5, f"Domain '{_domain}' needs at least 5 groups, has {len(_groups)}"

# ── Data Classes ───────────────────────────────────────────────────

@dataclass
class DomainMarket:
    """A market from any domain on Polymarket."""
    condition_id: str
    question: str
    event_title: str
    outcomes: list[str]
    outcome_prices: list[float]
    volume: float
    end_date: str
    neg_risk: bool
    token_ids: list[str]
    description: str = ""
    slug: str = ""
    domain: str = ""


@dataclass
class CrowdSignal:
    """Output of the crowd simulation."""
    market_id: str
    question: str
    polymarket_price: float
    crowd_probability: float
    confidence: float       # 0-1: how tight the consensus is
    edge: float
    side: str               # BUY_YES or BUY_NO
    kelly_size: float
    key_reasoning: str
    round1_estimates: list[float]
    round2_estimates: list[float]
    round3_final: float
    std_dev: float
    timestamp: str
    token_id: str = ""
    domain: str = ""


# ── Context Enrichment Per Domain ──────────────────────────────────

def _fetch_crypto_context(question: str, event_title: str) -> str:
    """Fetch crypto price data from CoinGecko and construct context."""
    parts = []
    text = (question + " " + event_title).lower()

    # Detect mentioned coins
    coin_map = {
        "bitcoin": "bitcoin", "btc": "bitcoin",
        "ethereum": "ethereum", "eth": "ethereum",
        "solana": "solana", "sol": "solana",
        "cardano": "cardano", "ada": "cardano",
        "ripple": "ripple", "xrp": "ripple",
        "dogecoin": "dogecoin", "doge": "dogecoin",
        "polygon": "matic-network",
        "avalanche": "avalanche-2",
        "chainlink": "chainlink", "link": "chainlink",
        "polkadot": "polkadot", "dot": "polkadot",
    }

    coins_to_fetch = set()
    for keyword, cg_id in coin_map.items():
        if keyword in text:
            coins_to_fetch.add(cg_id)

    # Always include BTC and ETH for context
    coins_to_fetch.update({"bitcoin", "ethereum"})

    # Fetch from CoinGecko (free, no API key)
    try:
        ids = ",".join(coins_to_fetch)
        resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids,
                "order": "market_cap_desc",
                "sparkline": "false",
                "price_change_percentage": "24h,7d,30d",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            coins = resp.json()
            parts.append("## Current Crypto Prices (CoinGecko)")
            for c in coins:
                name = c.get("name", "")
                symbol = c.get("symbol", "").upper()
                price = c.get("current_price", 0)
                mc = c.get("market_cap", 0)
                vol = c.get("total_volume", 0)
                chg_24h = c.get("price_change_percentage_24h", 0) or 0
                chg_7d = c.get("price_change_percentage_7d_in_currency", 0) or 0
                chg_30d = c.get("price_change_percentage_30d_in_currency", 0) or 0
                ath = c.get("ath", 0)
                ath_pct = c.get("ath_change_percentage", 0) or 0
                parts.append(
                    f"- {name} ({symbol}): ${price:,.2f} | "
                    f"24h: {chg_24h:+.1f}% | 7d: {chg_7d:+.1f}% | 30d: {chg_30d:+.1f}% | "
                    f"MCap: ${mc/1e9:.1f}B | Vol: ${vol/1e9:.1f}B | ATH: ${ath:,.0f} ({ath_pct:+.0f}%)"
                )
    except Exception as e:
        logger.debug(f"[CROWD-PRED] CoinGecko error: {e}")

    # Fetch Fear & Greed index
    try:
        resp = requests.get("https://api.alternative.me/fng/", timeout=5)
        if resp.status_code == 200:
            fng = resp.json().get("data", [{}])[0]
            parts.append(f"\n## Crypto Fear & Greed Index")
            parts.append(
                f"- Current: {fng.get('value', '?')} ({fng.get('value_classification', '?')})"
            )
    except Exception:
        pass

    return "\n".join(parts)


def _fetch_politics_context(question: str, event_title: str) -> str:
    """Construct political context from public data."""
    parts = []
    text = (question + " " + event_title).lower()

    parts.append("## Political Context (as of March 2026)")

    # General political context based on keywords detected
    if any(w in text for w in ["president", "presidential", "2028", "2026"]):
        parts.append("- Key factors: incumbent advantage, economic conditions, party unity")
        parts.append("- Consider: approval ratings, economic indicators, primary dynamics")

    if any(w in text for w in ["senate", "congress", "midterm", "house"]):
        parts.append("- Congressional races: consider generic ballot, redistricting effects")
        parts.append("- Historical midterm penalty for president's party averages -26 House seats")

    if any(w in text for w in ["approval", "rating"]):
        parts.append("- Approval rating context: 40-45% is danger zone for incumbents")
        parts.append("- Consider: polarization floor (~38-42%), rally effects, economic correlation")

    if any(w in text for w in ["supreme court", "scotus", "nomination"]):
        parts.append("- SCOTUS dynamics: consider nominee ideology, Senate composition")
        parts.append("- Historical confirmation rates: ~80% of nominees confirmed")

    parts.append("- Prediction market context: political markets often well-calibrated due to motivated traders")
    parts.append("- Key bias: recency bias, media narrative bias, enthusiasm vs turnout gap")

    return "\n".join(parts)


def _fetch_geopolitics_context(question: str, event_title: str) -> str:
    """Construct geopolitical context."""
    parts = []
    text = (question + " " + event_title).lower()

    parts.append("## Geopolitical Context (as of March 2026)")

    if any(w in text for w in ["russia", "ukraine"]):
        parts.append("- Russia-Ukraine: consider frontline dynamics, weapons supply, economic pressure")
        parts.append("- Key factors: NATO support levels, Russian mobilization capacity, energy markets")

    if any(w in text for w in ["china", "taiwan"]):
        parts.append("- China-Taiwan: consider military balance, economic interdependence, US commitment")
        parts.append("- Key factors: semiconductor dependency, PLA modernization, diplomatic signals")

    if any(w in text for w in ["israel", "palestine", "gaza", "hamas"]):
        parts.append("- Middle East: consider regional escalation risks, diplomatic initiatives")
        parts.append("- Key factors: US involvement, Iran proxy network, humanitarian situation")

    if any(w in text for w in ["iran", "nuclear"]):
        parts.append("- Iran nuclear: consider JCPOA status, enrichment levels, IAEA reports")
        parts.append("- Key factors: breakout time estimates, diplomatic channels, regional dynamics")

    if any(w in text for w in ["sanction", "tariff", "trade war"]):
        parts.append("- Economic warfare: consider enforcement effectiveness, evasion networks")
        parts.append("- Historical: sanctions succeed in ~30% of cases (Peterson Institute)")

    if any(w in text for w in ["nato", "alliance"]):
        parts.append("- NATO: consider burden-sharing debates, expansion dynamics, Article 5 credibility")

    # Oil prices as geopolitical context
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=5,
        )
        # Use oil proxy or just note it's a factor
    except Exception:
        pass

    parts.append("- Base rate: major geopolitical predictions are notoriously difficult")
    parts.append("- Prediction markets tend to overreact to dramatic events and underreact to slow trends")

    return "\n".join(parts)


def _fetch_entertainment_context(question: str, event_title: str) -> str:
    """Construct entertainment context."""
    parts = []
    text = (question + " " + event_title).lower()

    parts.append("## Entertainment Context (as of March 2026)")

    if any(w in text for w in ["oscar", "academy award"]):
        parts.append("- Oscars: precursor tracking is the strongest predictor")
        parts.append("- Key precursors: SAG ensemble → Best Picture (~75%), DGA → Director (~80%)")
        parts.append("- BAFTA increasingly predictive since membership reform")
        parts.append("- Preferential ballot favors consensus picks over divisive ones")

    if any(w in text for w in ["box office", "opening", "gross"]):
        parts.append("- Box office: tracking surveys + pre-sales are strongest predictors")
        parts.append("- Factors: marketing spend, competition, review embargo timing, franchise momentum")
        parts.append("- 2024+ trend: theatrical exclusivity shrinking, streaming window accelerating")

    if any(w in text for w in ["golden globe", "emmy", "grammy"]):
        parts.append("- Awards: consider voting body demographics, campaign spending")
        parts.append("- HFPA (Globes) is smaller/more volatile than Academy")

    if any(w in text for w in ["streaming", "netflix", "disney"]):
        parts.append("- Streaming wars context: subscriber growth slowing across platforms")
        parts.append("- Content spending rationalization, emphasis on profitability over growth")

    if any(w in text for w in ["album", "billboard", "chart"]):
        parts.append("- Music charts: streaming now dominates (70%+ of chart calculation)")
        parts.append("- Consider: playlist placement, TikTok virality, artist fanbase intensity")

    parts.append("- Entertainment prediction bias: fans overestimate their favorites")
    parts.append("- Markets often accurate for frontrunners but misjudge upsets")

    return "\n".join(parts)


def build_enrichment_context(market: DomainMarket) -> str:
    """Build domain-appropriate context for simulation prompts."""
    parts = []
    domain = market.domain

    # Domain-specific data enrichment
    if domain == "crypto":
        crypto_ctx = _fetch_crypto_context(market.question, market.event_title)
        if crypto_ctx:
            parts.append(crypto_ctx)
    elif domain == "politics":
        pol_ctx = _fetch_politics_context(market.question, market.event_title)
        if pol_ctx:
            parts.append(pol_ctx)
    elif domain == "geopolitics":
        geo_ctx = _fetch_geopolitics_context(market.question, market.event_title)
        if geo_ctx:
            parts.append(geo_ctx)
    elif domain == "entertainment":
        ent_ctx = _fetch_entertainment_context(market.question, market.event_title)
        if ent_ctx:
            parts.append(ent_ctx)

    # Market metadata (always included)
    parts.append(f"\n## Market Info")
    parts.append(f"- Question: {market.question}")
    parts.append(f"- Event: {market.event_title}")
    if market.description:
        parts.append(f"- Description: {market.description[:500]}")
    parts.append(f"- Outcomes: {', '.join(market.outcomes)}")
    parts.append(f"- Current prices: {', '.join(f'${p:.3f}' for p in market.outcome_prices)}")
    parts.append(f"- Volume: ${market.volume:,.0f}")
    parts.append(f"- Resolution: {market.end_date}")
    parts.append(f"- Domain: {domain}")

    return "\n".join(parts)


# ── LLM Communication ─────────────────────────────────────────────

def _call_llm(messages: list[dict], temperature: float = TEMPERATURE,
              max_tokens: int = 500) -> Optional[str]:
    """Call DeepSeek via LiteLLM proxy. ~$0.001 per call."""
    try:
        resp = requests.post(
            LITELLM_URL,
            headers={
                "Authorization": f"Bearer {LITELLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LITELLM_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        else:
            logger.warning(f"[CROWD-PRED] LLM error {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.warning(f"[CROWD-PRED] LLM call failed: {e}")
        return None


def _extract_probability(text: str) -> Optional[float]:
    """Extract probability from analyst response."""
    if not text:
        return None

    for pattern in [
        r'(?:PROBABILITY|ESTIMATE|CONSENSUS|FINAL):\s*(0\.\d+)',
        r'(?:PROBABILITY|ESTIMATE|CONSENSUS|FINAL):\s*(\d{1,3})%',
        r'\*\*(?:PROBABILITY|ESTIMATE|CONSENSUS|FINAL)\*\*:\s*(0\.\d+)',
        r'\*\*(?:PROBABILITY|ESTIMATE|CONSENSUS|FINAL)\*\*:\s*(\d{1,3})%',
    ]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = float(match.group(1))
            if val > 1:
                val /= 100
            if 0.01 <= val <= 0.99:
                return val

    # Fallback patterns
    for pattern in [
        r'probability\s+(?:is\s+)?(?:of\s+)?(?:about\s+)?(?:approximately\s+)?(0\.\d+)',
        r'(\d{1,2}(?:\.\d+)?)%\s*(?:probability|chance|likelihood)',
        r'(?:probability|chance|likelihood)\s+(?:of\s+)?(\d{1,2}(?:\.\d+)?)%',
        r'I\s+(?:estimate|assess|believe)\s+(?:a\s+)?(0\.\d+)',
    ]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = float(match.group(1))
            if val > 1:
                val /= 100
            if 0.01 <= val <= 0.99:
                return val

    return None


# ── Delphi Simulation Engine ──────────────────────────────────────

DOMAIN_LABELS = {
    "politics": "political",
    "crypto": "cryptocurrency/blockchain",
    "geopolitics": "geopolitical/international relations",
    "entertainment": "entertainment/media",
}


def _run_round1(market: DomainMarket, context: str,
                analyst_groups: list[dict]) -> list[tuple[str, float, str]]:
    """Round 1: Independent estimates from analyst groups."""
    results = []
    domain_label = DOMAIN_LABELS.get(market.domain, market.domain)

    for group in analyst_groups:
        system_prompt = (
            f"You are part of a group of 5 {group['name']}. "
            f"{group['role']}\n\n"
            f"You are predicting a {domain_label} outcome for a prediction market. "
            f"Your group must reach a consensus estimate.\n\n"
            f"IMPORTANT: You must end your response with exactly this format:\n"
            f"ESTIMATE: 0.XX\n"
            f"(a decimal between 0.01 and 0.99 representing the probability of the first outcome)"
        )

        user_prompt = (
            f"As a group of {group['name']}, estimate the probability of the "
            f"FIRST outcome happening.\n\n"
            f"Market question: {market.question}\n"
            f"Outcomes: {' vs '.join(market.outcomes)}\n\n"
            f"{context}\n\n"
            f"Think step by step as a team of 5 {group['name']} specialists. "
            f"Consider all relevant factors from your area of expertise. "
            f"Be specific — cite actual data, patterns, or precedents where possible. "
            f"Then agree on a final probability estimate.\n\n"
            f"End with: ESTIMATE: 0.XX"
        )

        response = _call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=TEMPERATURE,
            max_tokens=500,
        )

        if response:
            prob = _extract_probability(response)
            if prob is not None:
                reasoning = response.split("\n")[0][:200]
                results.append((group["name"], prob, reasoning))
                logger.debug(
                    f"[CROWD-PRED] Round 1 | {group['name']}: {prob:.3f}"
                )
            else:
                logger.debug(f"[CROWD-PRED] Round 1 | {group['name']}: no probability extracted")
        else:
            logger.debug(f"[CROWD-PRED] Round 1 | {group['name']}: LLM call failed")

        time.sleep(0.2)

    return results


def _run_round2(
    market: DomainMarket,
    context: str,
    round1_results: list[tuple[str, float, str]],
    analyst_groups: list[dict],
) -> list[tuple[str, float, str]]:
    """Round 2: Delphi revision. Groups see each other's estimates."""
    if not round1_results:
        return []

    r1_summary = "## Round 1 Results from All Analyst Groups:\n"
    for name, prob, reason in round1_results:
        r1_summary += f"- **{name}**: {prob:.3f} ({prob*100:.1f}%) -- {reason[:100]}\n"

    mean_r1 = sum(p for _, p, _ in round1_results) / len(round1_results)
    std_r1 = (sum((p - mean_r1)**2 for _, p, _ in round1_results) / len(round1_results)) ** 0.5
    r1_summary += f"\n**Round 1 average: {mean_r1:.3f} (std: {std_r1:.3f})**\n"

    results = []
    domain_label = DOMAIN_LABELS.get(market.domain, market.domain)

    for group in analyst_groups:
        own_r1 = None
        for name, prob, _ in round1_results:
            if name == group["name"]:
                own_r1 = prob
                break

        if own_r1 is None:
            continue

        system_prompt = (
            f"You are part of a group of 5 {group['name']}. "
            f"{group['role']}\n\n"
            f"This is Round 2 of a Delphi forecasting exercise on a {domain_label} question. "
            f"You can see all other groups' estimates from Round 1. "
            f"You may revise your estimate or keep it if you're confident.\n\n"
            f"IMPORTANT: End your response with exactly:\n"
            f"ESTIMATE: 0.XX"
        )

        user_prompt = (
            f"Market question: {market.question}\n"
            f"Outcomes: {' vs '.join(market.outcomes)}\n\n"
            f"YOUR Round 1 estimate was: {own_r1:.3f}\n\n"
            f"{r1_summary}\n\n"
            f"Now, seeing all groups' estimates, do you want to revise? "
            f"Consider:\n"
            f"1. Were there factors other groups considered that you missed?\n"
            f"2. Is there information asymmetry -- some groups have data you don't?\n"
            f"3. Should you move toward the consensus, or do you have strong reasons to disagree?\n"
            f"4. Beware of anchoring bias -- don't just average.\n\n"
            f"Provide your revised estimate (or keep the same if confident).\n"
            f"End with: ESTIMATE: 0.XX"
        )

        response = _call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=TEMPERATURE * 0.8,
            max_tokens=400,
        )

        if response:
            prob = _extract_probability(response)
            if prob is not None:
                reasoning = response.split("\n")[0][:200]
                results.append((group["name"], prob, reasoning))
                delta = prob - own_r1
                if abs(delta) > 0.02:
                    logger.debug(
                        f"[CROWD-PRED] Round 2 | {group['name']}: "
                        f"{own_r1:.3f} -> {prob:.3f} (delta {delta:+.3f})"
                    )

        time.sleep(0.2)

    return results


def _run_round3(
    market: DomainMarket,
    context: str,
    round1_results: list[tuple[str, float, str]],
    round2_results: list[tuple[str, float, str]],
) -> tuple[float, float, str]:
    """Round 3: Final consensus synthesis by a moderator."""
    r1_text = "\n".join(
        f"- {name}: {prob:.3f} -- {reason[:100]}"
        for name, prob, reason in round1_results
    )
    r2_text = "\n".join(
        f"- {name}: {prob:.3f} -- {reason[:100]}"
        for name, prob, reason in round2_results
    )

    r2_probs = [p for _, p, _ in round2_results] if round2_results else [p for _, p, _ in round1_results]
    if not r2_probs:
        return 0.5, 0.0, "No estimates available"

    mean_p = sum(r2_probs) / len(r2_probs)
    std_p = (sum((p - mean_p)**2 for p in r2_probs) / len(r2_probs)) ** 0.5

    r1_probs = [p for _, p, _ in round1_results]
    r1_std = (sum((p - sum(r1_probs)/len(r1_probs))**2 for p in r1_probs) / len(r1_probs)) ** 0.5 if r1_probs else 1.0

    domain_label = DOMAIN_LABELS.get(market.domain, market.domain)

    system_prompt = (
        f"You are the moderator of a Delphi forecasting panel on {domain_label} questions. "
        "You have seen two rounds of estimates from specialist groups. "
        "Your job is to synthesize a final consensus probability and assess confidence.\n\n"
        "IMPORTANT: End your response with exactly these two lines:\n"
        "PROBABILITY: 0.XX\n"
        "CONFIDENCE: 0.XX\n"
        "(confidence = how reliable this estimate is, 0.0 to 1.0)"
    )

    user_prompt = (
        f"Market question: {market.question}\n"
        f"Outcomes: {' vs '.join(market.outcomes)}\n\n"
        f"## Round 1 Independent Estimates:\n{r1_text}\n"
        f"Round 1 mean: {sum(r1_probs)/len(r1_probs):.3f}, std: {r1_std:.3f}\n\n"
        f"## Round 2 Revised Estimates:\n{r2_text}\n"
        f"Round 2 mean: {mean_p:.3f}, std: {std_p:.3f}\n\n"
        f"## Convergence: std went from {r1_std:.3f} to {std_p:.3f} "
        f"({'converged' if std_p < r1_std else 'diverged'})\n\n"
        f"Synthesize the final probability. Consider:\n"
        f"1. Weight groups that revised less (more confident in their expertise)\n"
        f"2. Weight groups that cited specific data over narratives\n"
        f"3. If std is low (<0.05), the crowd is confident -- trust the consensus\n"
        f"4. If std is high (>0.10), there's genuine uncertainty -- widen the estimate\n"
        f"5. Trim extreme outliers that didn't converge\n\n"
        f"Provide your final synthesis with 3 key reasons.\n\n"
        f"End with:\n"
        f"PROBABILITY: 0.XX\n"
        f"CONFIDENCE: 0.XX"
    )

    response = _call_llm(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=800,
    )

    if not response:
        # Fallback: trimmed mean
        sorted_probs = sorted(r2_probs)
        if len(sorted_probs) >= 4:
            trim = max(1, len(sorted_probs) // 5)
            trimmed = sorted_probs[trim:-trim]
            final_p = sum(trimmed) / len(trimmed)
        else:
            final_p = mean_p
        confidence = max(0.3, 1.0 - std_p * 5)
        return final_p, confidence, "Fallback: trimmed mean (LLM synthesis failed)"

    final_p = _extract_probability(response)
    if final_p is None:
        final_p = mean_p

    conf_match = re.search(r'CONFIDENCE:\s*(0\.\d+)', response, re.IGNORECASE)
    if conf_match:
        confidence = float(conf_match.group(1))
    else:
        confidence = max(0.3, 1.0 - std_p * 5)

    # Extract reasoning
    reasoning_lines = []
    for line in response.split("\n"):
        line = line.strip()
        if re.match(r'^[1-3][\.\)]\s', line) or line.startswith("- "):
            reasoning_lines.append(line[:150])
        if len(reasoning_lines) >= 3:
            break
    key_reasoning = " | ".join(reasoning_lines) if reasoning_lines else response[:300]

    return final_p, confidence, key_reasoning


def run_delphi_simulation(market: DomainMarket, context: str) -> Optional[CrowdSignal]:
    """Run the full 3-round Delphi simulation on any domain market."""
    domain = market.domain
    analyst_groups = DOMAIN_ANALYST_GROUPS.get(domain, [])
    if not analyst_groups:
        logger.warning(f"[CROWD-PRED] No analyst groups for domain '{domain}'")
        return None

    logger.info(
        f"[CROWD-PRED] [{domain.upper()}] Delphi simulation starting: "
        f"{market.question[:60]}... ({len(analyst_groups)} groups)"
    )

    # Round 1
    t0 = time.time()
    r1_results = _run_round1(market, context, analyst_groups)
    t1 = time.time()

    if len(r1_results) < 3:
        logger.warning(
            f"[CROWD-PRED] Round 1 too few estimates ({len(r1_results)}/{len(analyst_groups)}), aborting"
        )
        return None

    r1_probs = [p for _, p, _ in r1_results]
    r1_mean = sum(r1_probs) / len(r1_probs)
    logger.info(
        f"[CROWD-PRED] Round 1 ({t1-t0:.0f}s): {len(r1_results)} groups, "
        f"mean={r1_mean:.3f}, range=[{min(r1_probs):.3f}, {max(r1_probs):.3f}]"
    )

    # Round 2
    t2_start = time.time()
    r2_results = _run_round2(market, context, r1_results, analyst_groups)
    t2 = time.time()

    if len(r2_results) < 3:
        logger.warning(
            f"[CROWD-PRED] Round 2 too few revisions ({len(r2_results)}), using R1"
        )
        r2_results = r1_results

    r2_probs = [p for _, p, _ in r2_results]
    r2_mean = sum(r2_probs) / len(r2_probs)
    r2_std = (sum((p - r2_mean)**2 for p in r2_probs) / len(r2_probs)) ** 0.5
    logger.info(
        f"[CROWD-PRED] Round 2 ({t2-t2_start:.0f}s): {len(r2_results)} groups, "
        f"mean={r2_mean:.3f}, std={r2_std:.3f}"
    )

    # Round 3
    t3_start = time.time()
    final_p, confidence, key_reasoning = _run_round3(market, context, r1_results, r2_results)
    t3 = time.time()

    logger.info(
        f"[CROWD-PRED] Round 3 ({t3-t3_start:.0f}s): final={final_p:.3f}, "
        f"confidence={confidence:.2f}"
    )

    # Signal generation
    yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5

    if final_p > yes_price:
        edge = final_p - yes_price
        side = "BUY_YES"
        kelly = (final_p - yes_price) / (1 - yes_price) if yes_price < 1 else 0
    else:
        edge = (1 - final_p) - (1 - yes_price)
        side = "BUY_NO"
        kelly = ((1 - final_p) - (1 - yes_price)) / yes_price if yes_price > 0 else 0

    kelly_size = min(MAX_BET, max(5.0, kelly * KELLY_FRACTION * 1000 * confidence))

    # Select token_id
    if side == "BUY_YES" and len(market.token_ids) > 0:
        token_id = market.token_ids[0]
    elif side == "BUY_NO" and len(market.token_ids) > 1:
        token_id = market.token_ids[1]
    elif market.token_ids:
        token_id = market.token_ids[0]
    else:
        token_id = ""

    total_time = t3 - t0
    logger.info(
        f"[CROWD-PRED] [{domain.upper()}] COMPLETE ({total_time:.0f}s): "
        f"{market.question[:50]} | crowd={final_p:.3f} vs PM={yes_price:.3f} | "
        f"edge={edge:.3f} ({edge*100:.1f}%) | {side} ${kelly_size:.0f} | "
        f"conf={confidence:.2f}"
    )

    return CrowdSignal(
        market_id=market.condition_id,
        question=market.question[:100],
        polymarket_price=yes_price,
        crowd_probability=final_p,
        confidence=confidence,
        edge=round(edge, 4),
        side=side,
        kelly_size=round(kelly_size, 2),
        key_reasoning=key_reasoning[:500],
        round1_estimates=r1_probs,
        round2_estimates=r2_probs,
        round3_final=final_p,
        std_dev=round(r2_std, 4),
        timestamp=datetime.now(timezone.utc).isoformat(),
        token_id=token_id,
        domain=domain,
    )


# ── Market Discovery ──────────────────────────────────────────────

def _detect_domain(question: str, event_title: str) -> Optional[str]:
    """Detect which domain a market belongs to based on keywords."""
    text = (question + " " + event_title).lower()
    scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[domain] = score
    if not scores:
        return None
    return max(scores, key=scores.get)


def fetch_domain_markets(target_domain: str) -> list[DomainMarket]:
    """Fetch markets matching a specific domain from Polymarket."""
    all_markets = []

    try:
        resp = requests.get(
            GAMMA_EVENTS_API,
            params={"closed": "false", "limit": 100},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"[CROWD-PRED] Events API {resp.status_code}")
            return []

        events = resp.json()
        for event in events:
            title = event.get("title", "")
            slug = event.get("slug", "")

            for m in event.get("markets", []):
                try:
                    question = m.get("question", "") or m.get("title", "")
                    domain = _detect_domain(question, title)
                    if domain != target_domain:
                        continue

                    cid = m.get("conditionId") or m.get("condition_id", "")
                    vol = float(m.get("volume", 0) or 0)
                    if not cid or vol < MIN_VOLUME:
                        continue

                    outcomes = m.get("outcomes", ["Yes", "No"])
                    if isinstance(outcomes, str):
                        try:
                            outcomes = json.loads(outcomes)
                        except Exception:
                            outcomes = ["Yes", "No"]

                    prices_raw = m.get("outcomePrices", [])
                    if isinstance(prices_raw, str):
                        try:
                            prices_raw = json.loads(prices_raw)
                        except Exception:
                            prices_raw = []
                    prices = [float(p) for p in prices_raw] if prices_raw else []

                    token_ids = []
                    clob_ids = m.get("clobTokenIds", [])
                    if isinstance(clob_ids, str):
                        try:
                            clob_ids = json.loads(clob_ids)
                        except Exception:
                            clob_ids = []
                    token_ids = clob_ids if clob_ids else []

                    all_markets.append(DomainMarket(
                        condition_id=cid,
                        question=question,
                        event_title=title,
                        outcomes=outcomes,
                        outcome_prices=prices,
                        volume=vol,
                        end_date=m.get("endDate", ""),
                        neg_risk=m.get("negRisk", False),
                        token_ids=token_ids,
                        description=(m.get("description", "") or "")[:500],
                        slug=m.get("slug", ""),
                        domain=target_domain,
                    ))
                except Exception as e:
                    logger.debug(f"[CROWD-PRED] Market parse error: {e}")

    except Exception as e:
        logger.error(f"[CROWD-PRED] Events API error: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for m in all_markets:
        if m.condition_id not in seen:
            seen.add(m.condition_id)
            unique.append(m)

    logger.info(
        f"[CROWD-PRED] [{target_domain.upper()}] Found {len(unique)} markets "
        f"(vol >= ${MIN_VOLUME:,})"
    )
    return unique


# ── Cache Management ───────────────────────────────────────────────

def _cache_key(market_id: str, domain: str) -> str:
    raw = f"{domain}:{market_id}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _get_cached_signal(market_id: str, domain: str) -> Optional[CrowdSignal]:
    """Check if we have a recent simulation for this market."""
    cache_file = CACHE_DIR / f"{_cache_key(market_id, domain)}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > CACHE_TTL:
            return None
        return CrowdSignal(**data)
    except Exception:
        return None


def _save_cached_signal(signal: CrowdSignal):
    """Cache a simulation result."""
    cache_file = CACHE_DIR / f"{_cache_key(signal.market_id, signal.domain)}.json"
    cache_file.write_text(json.dumps(asdict(signal), indent=2))


# ── Strategy Class ─────────────────────────────────────────────────

class CrowdPredictionStrategy:
    """
    Multi-Domain Crowd Simulation strategy using Delphi method.
    Supports: politics, crypto, geopolitics, entertainment.
    Each domain has 5-10 specialist groups with domain-specific expertise.
    """

    # AutoOptimizer parameter space
    MIN_EDGE = MIN_EDGE
    MAX_BET = MAX_BET
    KELLY_FRACTION = KELLY_FRACTION
    MIN_VOLUME = MIN_VOLUME
    MAX_MARKETS_PER_SCAN = MAX_MARKETS_PER_SCAN

    DOMAINS = ["politics", "crypto", "geopolitics", "entertainment"]

    def __init__(self, api=None, risk=None, domains: list[str] = None):
        self.api = api
        self.risk = risk
        self._domains = domains or self.DOMAINS
        self._total_predictions = 0
        self._total_trades = 0
        self._total_pnl = 0.0
        self._last_scan = 0.0
        self._domain_stats: dict[str, dict] = {
            d: {"predictions": 0, "trades": 0, "signals": 0}
            for d in self._domains
        }
        self._predictions_history: list[dict] = []

        # Load history
        if RESULTS_FILE.exists():
            try:
                self._predictions_history = json.loads(RESULTS_FILE.read_text())
            except Exception:
                pass

    def scan_domain(self, domain: str, shared_markets: list = None) -> list[CrowdSignal]:
        """
        Scan markets for a specific domain, run crowd simulation, return signals.

        Args:
            domain: one of politics, crypto, geopolitics, entertainment
            shared_markets: ignored -- we fetch domain markets from Gamma events API
        """
        if domain not in DOMAIN_ANALYST_GROUPS:
            logger.warning(f"[CROWD-PRED] Unknown domain: {domain}")
            return []

        # Check LiteLLM proxy
        try:
            resp = requests.get("http://localhost:4000/health", timeout=5)
            if resp.status_code != 200:
                logger.debug("[CROWD-PRED] LiteLLM proxy not available")
                return []
        except Exception:
            logger.debug("[CROWD-PRED] LiteLLM proxy not reachable")
            return []

        # Fetch domain markets
        markets = fetch_domain_markets(domain)
        if not markets:
            logger.info(f"[CROWD-PRED] [{domain.upper()}] No markets found")
            return []

        # Sort by volume, take top N
        markets.sort(key=lambda m: m.volume, reverse=True)
        markets = markets[:self.MAX_MARKETS_PER_SCAN]

        signals = []
        for i, market in enumerate(markets):
            # Check cache
            cached = _get_cached_signal(market.condition_id, domain)
            if cached:
                if cached.edge >= self.MIN_EDGE:
                    current_price = market.outcome_prices[0] if market.outcome_prices else 0.5
                    if abs(current_price - cached.polymarket_price) > 0.03:
                        logger.info(
                            f"[CROWD-PRED] Cache stale (price moved), re-simulating"
                        )
                    else:
                        signals.append(cached)
                        logger.info(
                            f"[CROWD-PRED] [{domain.upper()}] [{i+1}/{len(markets)}] "
                            f"CACHED: {market.question[:50]} | edge={cached.edge:.1%}"
                        )
                        continue
                else:
                    logger.debug(
                        f"[CROWD-PRED] [{domain.upper()}] Cached no-edge: {market.question[:40]}"
                    )
                    continue

            # Enrich with domain-specific context
            logger.info(
                f"[CROWD-PRED] [{domain.upper()}] [{i+1}/{len(markets)}] "
                f"Simulating: {market.question[:60]}"
            )
            context = build_enrichment_context(market)

            # Run Delphi
            signal = run_delphi_simulation(market, context)
            if signal:
                self._total_predictions += 1
                self._domain_stats[domain]["predictions"] += 1
                _save_cached_signal(signal)

                if signal.edge >= self.MIN_EDGE:
                    signals.append(signal)
                    self._domain_stats[domain]["signals"] += 1
                    logger.info(
                        f"[CROWD-PRED] [{domain.upper()}] SIGNAL: {signal.side} "
                        f"edge={signal.edge:.1%} crowd={signal.crowd_probability:.3f} "
                        f"vs PM={signal.polymarket_price:.3f} conf={signal.confidence:.2f} "
                        f"size=${signal.kelly_size:.0f} | {market.question[:50]}"
                    )
                else:
                    logger.info(
                        f"[CROWD-PRED] [{domain.upper()}] No edge: "
                        f"crowd={signal.crowd_probability:.3f} vs PM={signal.polymarket_price:.3f} "
                        f"(edge={signal.edge:.1%} < {self.MIN_EDGE:.0%}) | {market.question[:50]}"
                    )
            else:
                logger.warning(
                    f"[CROWD-PRED] [{domain.upper()}] Simulation failed: {market.question[:50]}"
                )

        # Save history
        if signals:
            for s in signals:
                self._predictions_history.append(asdict(s))
            self._predictions_history = self._predictions_history[-500:]
            RESULTS_FILE.write_text(json.dumps(self._predictions_history, indent=2))

        self._last_scan = time.time()
        logger.info(
            f"[CROWD-PRED] [{domain.upper()}] Scan complete: {len(markets)} markets -> "
            f"{len(signals)} signals"
        )

        return signals

    def scan(self, shared_markets: list = None, domain: str = None) -> list[CrowdSignal]:
        """
        Bot main loop compatible scan.
        If domain specified, scan that domain. Otherwise scan all domains.
        """
        if domain:
            return self.scan_domain(domain, shared_markets)

        # Scan all domains
        all_signals = []
        for d in self._domains:
            try:
                signals = self.scan_domain(d, shared_markets)
                all_signals.extend(signals)
            except Exception as e:
                logger.error(f"[CROWD-PRED] [{d.upper()}] Scan error: {e}", exc_info=True)

        return all_signals

    def execute(self, signal: CrowdSignal, api=None, risk=None,
                live: bool = False) -> bool:
        """Execute a crowd prediction trade."""
        api = api or self.api
        risk = risk or self.risk

        if not api or not risk:
            logger.error("[CROWD-PRED] No API or risk manager")
            return False

        size = min(signal.kelly_size, self.MAX_BET)
        can, reason = risk.can_trade(
            strategy="crowd_prediction",
            size=size,
            price=signal.polymarket_price,
            side=signal.side,
            market_id=signal.market_id,
        )
        if not can:
            logger.info(f"[CROWD-PRED] Trade blocked: {reason}")
            return False

        if not live:
            # Paper trade
            logger.info(
                f"[CROWD-PRED] [{signal.domain.upper()}] PAPER {signal.side} ${size:.2f} @ "
                f"{signal.polymarket_price:.3f} | edge={signal.edge:.1%} "
                f"conf={signal.confidence:.2f} | {signal.question[:50]}"
            )
            trade = Trade(
                timestamp=time.time(),
                strategy="crowd_prediction",
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side,
                size=size,
                price=signal.polymarket_price,
                edge=signal.edge,
                reason=f"crowd_delphi_{signal.domain} conf={signal.confidence:.2f} std={signal.std_dev:.3f}",
            )
            risk.open_trade(trade)
            self._total_trades += 1
            self._domain_stats.get(signal.domain, {})["trades"] = \
                self._domain_stats.get(signal.domain, {}).get("trades", 0) + 1
            return True

        # Live execution
        token_id = signal.token_id
        if not token_id:
            logger.error(f"[CROWD-PRED] No token_id for {signal.question[:40]}")
            return False

        target_price = signal.polymarket_price
        logger.info(
            f"[CROWD-PRED] [{signal.domain.upper()}] LIVE {signal.side} ${size:.2f} @ "
            f"{target_price:.3f} | edge={signal.edge:.1%} | {signal.question[:50]}"
        )

        try:
            result = api.smart_buy(
                token_id=token_id,
                amount=size,
                target_price=target_price,
            )

            if result:
                trade = Trade(
                    timestamp=time.time(),
                    strategy="crowd_prediction",
                    market_id=signal.market_id,
                    token_id=token_id,
                    side=signal.side,
                    size=size,
                    price=target_price,
                    edge=signal.edge,
                    reason=f"crowd_delphi_{signal.domain} conf={signal.confidence:.2f} std={signal.std_dev:.3f}",
                )
                risk.open_trade(trade)
                self._total_trades += 1
                self._domain_stats.get(signal.domain, {})["trades"] = \
                    self._domain_stats.get(signal.domain, {}).get("trades", 0) + 1
                logger.info(
                    f"[CROWD-PRED] [{signal.domain.upper()}] FILLED: "
                    f"{signal.side} ${size:.2f} | {signal.question[:50]}"
                )
                return True
            else:
                logger.warning(
                    f"[CROWD-PRED] Order failed: {signal.side} ${size:.2f} | "
                    f"{signal.question[:50]}"
                )
                return False

        except Exception as e:
            logger.error(f"[CROWD-PRED] Execution error: {e}")
            return False

    @property
    def stats(self) -> dict:
        """Return strategy statistics."""
        return {
            "total_predictions": self._total_predictions,
            "total_trades": self._total_trades,
            "total_pnl": round(self._total_pnl, 2),
            "predictions_cached": len(list(CACHE_DIR.glob("*.json"))),
            "domain_stats": self._domain_stats,
            "last_scan": datetime.fromtimestamp(self._last_scan).isoformat() if self._last_scan else "never",
        }


# ── Standalone CLI ─────────────────────────────────────────────────

def main():
    """Run as standalone script for testing."""
    import argparse

    parser = argparse.ArgumentParser(description="Crowd Prediction (Multi-Domain Delphi)")
    parser.add_argument("--status", action="store_true", help="Show prediction history")
    parser.add_argument("--scan", action="store_true", help="Run a single scan")
    parser.add_argument("--domain", type=str, default=None,
                        choices=["politics", "crypto", "geopolitics", "entertainment"],
                        help="Scan specific domain (default: all)")
    parser.add_argument("--limit", type=int, default=5, help="Max markets per domain")
    parser.add_argument("--daemon", action="store_true", help="Run continuously (every 4h)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
    )

    if args.status:
        if RESULTS_FILE.exists():
            preds = json.loads(RESULTS_FILE.read_text())
            print(f"\n{'='*70}")
            print(f"CROWD PREDICTION — {len(preds)} total")
            print(f"{'='*70}")
            for p in preds[-15:]:
                domain = p.get('domain', '?')
                print(
                    f"  [{domain.upper():14s}] {p['side']} edge={p['edge']:.1%} "
                    f"crowd={p['crowd_probability']:.3f} PM={p['polymarket_price']:.3f} "
                    f"${p['kelly_size']:.0f} conf={p['confidence']:.2f} "
                    f"std={p['std_dev']:.3f} | {p['question'][:50]}"
                )
                if p.get('key_reasoning'):
                    print(f"    Reasoning: {p['key_reasoning'][:100]}")
            print(f"{'='*70}")
        else:
            print("No predictions yet.")
        return

    strategy = CrowdPredictionStrategy()
    strategy.MAX_MARKETS_PER_SCAN = args.limit

    if args.daemon:
        logger.info(f"[CROWD-PRED] Daemon mode -- scan every 4h, limit={args.limit}")
        cycle = 0
        domains = ["politics", "crypto", "geopolitics", "entertainment"]
        while True:
            domain = domains[cycle % len(domains)]
            try:
                signals = strategy.scan_domain(domain)
                for s in signals:
                    logger.info(
                        f"  -> [{s.domain}] {s.side} {s.question[:50]} "
                        f"edge={s.edge:.1%} ${s.kelly_size:.0f}"
                    )
            except Exception as e:
                logger.error(f"[CROWD-PRED] Scan error: {e}")
            cycle += 1
            time.sleep(1 * 3600)  # 1h between rotations = 4h per domain
    else:
        if args.domain:
            signals = strategy.scan_domain(args.domain)
        else:
            signals = strategy.scan()
        print(f"\n{len(signals)} signals found:")
        for s in signals:
            print(
                f"  [{s.domain}] {s.side} edge={s.edge:.1%} "
                f"crowd={s.crowd_probability:.3f} PM={s.polymarket_price:.3f} "
                f"${s.kelly_size:.0f} conf={s.confidence:.2f} | {s.question[:60]}"
            )
            print(f"    Reasoning: {s.key_reasoning[:120]}")
            print(f"    R1: {s.round1_estimates}")
            print(f"    R2: {s.round2_estimates}")


if __name__ == "__main__":
    main()
