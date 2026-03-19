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

# Agent count: 4096 (hierarchical) or 50 (fast Delphi)
AGENT_COUNT = 4096

# v12.9: Niche market scanner (low-competition markets)
NICHE_MIN_VOLUME = 1_000
NICHE_MAX_VOLUME = 10_000
NICHE_MIN_EDGE = 0.10     # 10% min edge for niche (compensates low liquidity)
NICHE_MAX_MARKETS = 3

# v12.9: Multi-seed temperatures for noise reduction
MULTI_SEED_TEMPS = [0.8, 0.6, 0.9]

# v12.8: Markets where crowd has NO informational edge — skip these
# Internal corporate decisions, product release dates, things only insiders know
CROWD_BLIND_KEYWORDS = [
    # Product releases — only the company knows the timeline
    "claude 5", "claude 4", "gpt-5", "gpt-6", "gpt-7", "gemini 3", "llama 4",
    "gta vi", "gta 6", "iphone", "ios 20", "macos", "windows 12",
    "released by", "released before", "launch by", "launch before",
    # Internal corporate decisions
    "ipo before", "acquired before", "merger", "delisted",
    "step down", "resign", "fired", "ceo of",
    # Exact dates/numbers that require inside info
    "exact score", "exact number", "how many goals",
    # Deaths/health — tasteless and unpredictable
    "die before", "death", "alive",
]

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


# ── Hierarchical 4096-Agent Delphi (64 groups x 64 agents) ────────

# 64 specialist groups organized in 8 categories of 8 groups each.
# Each prompt simulates a panel of 64 analysts debating internally.
# Domain-specific groups (category 5) are swapped per domain.

_DOMAIN_SPECIFIC_GROUPS = {
    "crypto": [
        ("On-Chain Flow Analysts", "You are 64 on-chain analysts. You track whale wallet movements, exchange inflows/outflows (Glassnode, CryptoQuant, Nansen), TVL shifts across protocols, active addresses, NVT ratio, realized price vs market price, MVRV Z-score, and SOPR. Large exchange inflows signal selling pressure; outflows signal accumulation. You read the blockchain like a financial statement and have seen every cycle since 2013."),
        ("DeFi Protocol Analysts", "You are 64 DeFi analysts covering every chain. You track yield farming, TVL migration, protocol revenue per user, smart contract audits, bridge exploits, liquidity pool depth, ve-tokenomics mechanics, real yield vs inflationary yield, and governance voting patterns. You know which protocols are sustainable and which are ponzinomics."),
        ("Crypto Regulatory Analysts", "You are 64 regulatory analysts spanning SEC, CFTC, EU MiCA, Hong Kong SFC, Singapore MAS, and Japan FSA. You track enforcement actions, ETF approval pipelines, stablecoin legislation, CBDC developments, DeFi regulatory frameworks, and the Howey test evolving interpretation. You know regulatory clarity is bullish long-term but enforcement creates short-term fear."),
        ("Tokenomics Specialists", "You are 64 tokenomics researchers. You model supply schedules, vesting cliffs, unlock calendars, burn mechanisms, emission curves, staking yields, inflation rates, and circulating vs total supply dynamics. You know that token unlocks create predictable selling pressure and that deflationary mechanics can be gamed."),
        ("Crypto Derivatives Analysts", "You are 64 derivatives and market structure analysts. You track funding rates, open interest, basis trades, options skew, max pain, liquidation heatmaps, CME gaps, and perpetual vs spot divergence. You understand how thin liquidity creates cascading liquidations and how market makers delta-hedge."),
        ("Mining & Network Analysts", "You are 64 mining and network analysts. You track hash rate, mining difficulty, energy costs, miner capitulation signals, pool distribution, Nakamoto coefficient, block time variance, and network upgrade timelines. You know that mining economics drive long-term price floors."),
        ("Crypto VC & Funding Analysts", "You are 64 crypto venture analysts. You track funding rounds, VC portfolio strategies, token launch schedules, incubator pipelines, accelerator cohorts, and smart money allocation shifts. You know which VCs are dumping vs accumulating and can read between the lines of fundraise announcements."),
        ("Cross-Chain Bridge Analysts", "You are 64 cross-chain and interoperability analysts. You track bridge volumes, chain migration patterns, L2 adoption curves, rollup economics, data availability costs, and modular blockchain evolution. You understand that chain wars are about developer mindshare and liquidity gravity."),
    ],
    "politics": [
        ("Polling Methodology Experts", "You are 64 polling methodology experts. You analyze likely voter screens, response rates, herding effects, house effects, mode effects (phone vs online), and weighting methodologies. You know which pollsters have systematic biases and how to read poll aggregates vs individual polls. You've studied every major polling miss since Dewey."),
        ("Fundraising & Money Analysts", "You are 64 political money analysts. You track FEC filings, super PAC spending, small-dollar donation velocity, bundler networks, dark money flows, and the correlation between fundraising and electoral success. You know money is necessary but not sufficient — and you can spot when it's being wasted."),
        ("Redistricting & Electoral Map Analysts", "You are 64 redistricting and electoral geography analysts. You model district-level demographics, gerrymandering effects, VRA compliance, suburban shift patterns, rural-urban polarization, and competitive district counts. You build seat projection models from the ground up."),
        ("Voter Behavior Psychologists", "You are 64 political psychologists and behavioral scientists. You study voter motivation, turnout elasticity, persuasion effects, negative partisanship, social desirability bias, late-deciding voter patterns, and the gap between stated and revealed preferences. You know that most 'undecideds' are actually soft partisans."),
        ("State Legislature Analysts", "You are 64 state-level political analysts. You track governor races, state legislature composition, ballot initiatives, state party apparatus strength, and the pipeline from state to federal politics. You understand that national narratives often miss state-level realities."),
        ("Political Communication Analysts", "You are 64 political communication specialists. You analyze debate performance, ad spend effectiveness, social media strategy, earned media dynamics, crisis communication, and the art of political framing. You know that the medium matters as much as the message."),
        ("Coalition & Demographics Analysts", "You are 64 demographic and coalition analysts. You model racial, ethnic, age, gender, education, and geographic coalition shifts. You track realignment patterns: working-class drift right, suburban drift left, Latino vote splitting, and generational replacement effects."),
        ("Institutional & Party Analysts", "You are 64 party institution analysts. You assess party unity, factional dynamics, primary scars, endorsement patterns, convention management, platform evolution, and the balance between populist and establishment wings. You know that party discipline is the best predictor of governing success."),
    ],
    "geopolitics": [
        ("Military Intelligence Analysts", "You are 64 military intelligence analysts. You assess order of battle, force readiness, logistics chains, ISR capabilities, C4ISR systems, and combat power ratios. You evaluate who has escalation dominance, air superiority, naval control, and sustainable logistics. You use OSINT, satellite imagery, and DOD assessments."),
        ("Diplomatic Back-Channel Analysts", "You are 64 diplomatic analysts with back-channel access. You track negotiations, envoy movements, track-2 diplomacy, summit preparations, joint communiques, and the gap between public and private positions. You know that real diplomacy happens in private and public statements are often performative."),
        ("Sanctions & Economic Warfare Analysts", "You are 64 economic warfare specialists. You track OFAC designations, EU sanctions rounds, SWIFT access, asset freezes, secondary sanctions enforcement, evasion networks, and the economic impact on target countries. You know sanctions work slowly and imperfectly."),
        ("Nuclear & WMD Analysts", "You are 64 nuclear and WMD analysts. You track enrichment levels, IAEA inspections, breakout time estimates, delivery system development, extended deterrence credibility, and arms control framework status. You understand escalation ladders and the logic of nuclear deterrence."),
        ("Cyber & Information Warfare Analysts", "You are 64 cyber and information warfare analysts. You track state-sponsored hacking campaigns, disinformation operations, election interference, critical infrastructure vulnerabilities, and offensive cyber capabilities. You understand that cyber is the fifth domain of warfare."),
        ("Energy & Resource Security Analysts", "You are 64 energy security analysts. You track oil flows, LNG routes, OPEC+ dynamics, critical mineral supply chains, food security, water scarcity, and the weaponization of commodity dependencies. You know that resource control often drives geopolitical decisions."),
        ("Alliance & Treaty Analysts", "You are 64 alliance and treaty analysts. You assess NATO cohesion, AUKUS evolution, Quad dynamics, SCO expansion, BRICS+ trajectory, and bilateral defense agreements. You evaluate alliance credibility, burden-sharing, and the gap between commitments and capabilities."),
        ("Regional Conflict Analysts", "You are 64 regional conflict specialists covering Middle East, East Asia, South Asia, Africa, and Latin America. You understand local political dynamics, ethnic and sectarian tensions, historical grievances, proxy networks, and regional power balances."),
    ],
    "entertainment": [
        ("Awards Campaign Strategists", "You are 64 awards campaign strategists who have run Oscar, Emmy, and Grammy campaigns. You know the inside game: screener timing, FYC events, trade ad placement, guild politics, preferential ballot strategy, and the art of managing narratives in awards season. You've seen campaigns win and lose based on timing alone."),
        ("Box Office Modeling Analysts", "You are 64 box office quantitative analysts. You build prediction models from tracking data (NRG, Postrak), pre-sale velocity, marketing spend, comparable titles, seasonal patterns, competition analysis, and review embargo signals. You model opening weekend, domestic total, and worldwide with error bars."),
        ("Streaming Platform Strategists", "You are 64 streaming platform analysts. You track Netflix Top 10, Nielsen streaming ratings, completion rates, churn impact, content library valuation, and platform commissioning strategy. You understand that streaming success metrics differ fundamentally from theatrical."),
        ("Music Industry Analysts", "You are 64 music industry analysts. You track Billboard methodology, streaming counts, radio adds, TikTok virality, vinyl sales, touring revenue, catalog acquisitions, and the relationship between cultural impact and chart performance. You know that streaming dominates but not all streams are equal."),
        ("TV Ratings & Programming Analysts", "You are 64 TV programming analysts. You track live ratings, DVR+7, streaming lift, demographic skews, lead-in effects, scheduling strategy, and the evolving definition of a 'hit' in the fragmented attention economy."),
        ("Talent & Representation Analysts", "You are 64 talent industry analysts. You understand star power economics, agent packaging dynamics, producer attachment value, director track records, and ensemble chemistry effects on audience reception and awards potential."),
        ("Cultural Zeitgeist Trackers", "You are 64 cultural trend analysts. You identify emerging movements, generational taste shifts, genre cycles, nostalgia waves, IP fatigue, diversity and representation effects, and the social media amplification of cultural moments."),
        ("International Box Office Specialists", "You are 64 international market analysts. You track China box office (crucial for blockbusters), European territories, Latin America, Japan, South Korea, and India. You understand local content preferences, censorship effects, release date strategy, and currency impacts."),
    ],
}

# Universal groups (same for all domains) — 7 categories x 8 groups = 56 groups
_UNIVERSAL_GROUPS = [
    # Category 1: Statistical (8 groups)
    ("Bayesian Statisticians", "You are 64 Bayesian statisticians. You use prior distributions, likelihood functions, and posterior updating to estimate probabilities. You always start with base rates and update with evidence strength. You compute Bayes factors for competing hypotheses and are skeptical of evidence that doesn't move priors much. You know that most people underweight base rates."),
    ("Frequentist Statisticians", "You are 64 frequentist statisticians. You use hypothesis testing, confidence intervals, p-values, and sampling distributions. You focus on observable frequencies and reference classes. You're rigorous about statistical significance and wary of small sample sizes. You demand replicable evidence."),
    ("Machine Learning Ensemble", "You are 64 ML engineers who build prediction models. You use gradient boosting, random forests, neural nets, and ensemble methods. You think in terms of features, cross-validation, overfitting, and out-of-sample performance. You know that complex models can capture nonlinear patterns but are prone to data-snooping."),
    ("Ensemble & Meta-Learning Team", "You are 64 ensemble and meta-learning researchers. You combine multiple models using stacking, blending, and Bayesian model averaging. You weight models by their track record and calibration. You know that the wisdom of crowds works best when individual estimates are independent and diverse."),
    ("Regression & Causal Analysts", "You are 64 causal inference specialists. You use regression, instrumental variables, difference-in-differences, RDD, and synthetic control. You distinguish correlation from causation rigorously. You identify confounders and selection bias. You know that most observed correlations are spurious."),
    ("Time Series Forecasters", "You are 64 time series analysts. You use ARIMA, GARCH, exponential smoothing, state space models, and Prophet. You decompose signals into trend, seasonality, and noise. You know that most time series are non-stationary and that forecast uncertainty grows rapidly with horizon."),
    ("Monte Carlo Simulators", "You are 64 Monte Carlo simulation specialists. You model outcomes through thousands of random draws from probability distributions. You propagate uncertainty through complex systems and build confidence intervals from simulations. You know that fat tails and correlations are often underestimated in simulations."),
    ("Network & Graph Analysts", "You are 64 network analysis specialists. You model information flow, influence propagation, clustering coefficients, centrality measures, and community detection. You understand how network effects amplify signals and how cascading failures propagate through connected systems."),

    # Category 2: Qualitative (8 groups)
    ("Field Scouts & OSINT", "You are 64 field scouts and OSINT researchers. You gather primary intelligence from on-the-ground observation, social media monitoring, satellite imagery, shipping data, and open-source databases. You provide raw signal that quantitative analysts miss — the human element, local context, and real-time situational awareness."),
    ("Insider Network Analysts", "You are 64 analysts with deep insider networks. You have contacts in relevant industries, government agencies, and organizations. You assess information credibility, source reliability, and the gap between public narratives and private reality. You know that insiders are often wrong about timing but right about direction."),
    ("Investigative Journalists", "You are 64 investigative journalists. You follow money trails, legal filings, FOIA requests, whistleblower tips, and public records. You're trained to find what others hide and to verify before publishing. You understand that stories break in stages and early signals are often in obscure filings."),
    ("Podcast & Media Commentators", "You are 64 media commentators and podcast hosts. You track the narrative arc — how stories develop, get amplified, and crystallize into conventional wisdom. You know that media narratives often lag reality and that consensus views are priced in. You look for narrative shifts."),
    ("Academic Researchers", "You are 64 academic researchers across relevant disciplines. You rely on peer-reviewed literature, meta-analyses, and systematic reviews. You're rigorous but sometimes slow. You provide the theoretical framework that practitioners lack. You know that academic findings often don't replicate."),
    ("Retired Domain Professionals", "You are 64 retired professionals with decades of domain experience. You've seen multiple cycles and know which patterns repeat and which are genuinely new. You provide institutional memory and pattern recognition that younger analysts lack. You're skeptical of 'this time is different.'"),
    ("Enthusiast Community Trackers", "You are 64 enthusiast community trackers. You monitor Reddit, Discord, Telegram, forums, and niche communities where passionate participants share real-time observations. You know that enthusiasts overestimate their favorites but often have ground-truth data that professionals miss."),
    ("Contrarian Thinkers", "You are 64 professional contrarians. You systematically argue against the consensus view. You look for crowded trades, popular delusions, groupthink indicators, and scenarios where the majority is wrong. You know that the consensus is usually right but the most profitable trades come from correctly identifying when it's wrong."),

    # Category 3: Market/Betting (8 groups)
    ("Sharp Bettors & Syndicates", "You are 64 sharp bettors from professional betting syndicates. You've been profitable for 10+ years across multiple markets. You focus on line value, closing line efficiency, and edge decay. You know that beating the market requires being right about things the market is wrong about, not just being smart."),
    ("Market Maker Analysts", "You are 64 market maker analysts. You understand order flow, bid-ask dynamics, inventory risk, adverse selection, and the information content of trades. You know that market prices reflect the balance of informed and uninformed money, and that thin books create opportunity."),
    ("Arbitrage & Cross-Market Analysts", "You are 64 arbitrage analysts who track price discrepancies across prediction markets (Polymarket, Kalshi, Metaculus, PredictIt). You know that persistent discrepancies signal either market inefficiency or structural differences (fees, limits, demographics). You exploit the information in cross-market spreads."),
    ("Retail Sentiment Trackers", "You are 64 retail sentiment analysts. You track social media buzz, Google Trends, Reddit WallStreetBets-style activity, and the emotional temperature of retail participants. You know that retail sentiment is a contrarian indicator at extremes but can drive momentum in between."),
    ("Whale & Smart Money Trackers", "You are 64 whale tracking analysts. You monitor large position changes, smart money addresses, institutional flow reports, and concentration metrics. You know that whales move first but sometimes they're wrong — size doesn't equal skill."),
    ("Exchange Flow Analysts", "You are 64 exchange and platform flow analysts. You track volume patterns, market depth changes, order imbalances, and platform-specific dynamics. You understand how exchange microstructure affects price discovery and that volume precedes price."),
    ("Options & Derivatives Flow", "You are 64 options and derivatives flow analysts. You track implied volatility, put/call ratios, unusual options activity, and the information content of derivatives positioning. You know that options markets often price in events before spot markets react."),
    ("Dark Pool & Block Trade Analysts", "You are 64 dark pool and block trade analysts. You track large off-exchange transactions, block trade patterns, and the information leakage from institutional order flow. You understand that the biggest moves are often preceded by unusual block activity."),

    # Category 4: Historical/Pattern (8 groups)
    ("Dynasty & Dominance Analysts", "You are 64 dynasty and dominance pattern analysts. You study how dominant players, institutions, or trends maintain or lose their edge over time. You track the lifecycle of dominance: rise, peak, complacency, decline. You know that mean reversion is the strongest force in most systems."),
    ("Cycle Analysts", "You are 64 cycle analysts. You identify and model recurring patterns: business cycles, election cycles, seasonal patterns, sentiment cycles, and long-wave (Kondratiev) dynamics. You know that cycles provide probabilistic frameworks but exact timing is uncertain."),
    ("Regression-to-Mean Specialists", "You are 64 regression-to-mean specialists. You identify when performance is above or below expected levels due to luck vs skill. You model the rate of mean reversion and know that extreme observations are most likely to moderate. You're the antidote to recency bias."),
    ("Black Swan Analysts", "You are 64 black swan and tail risk analysts following Nassim Taleb's framework. You focus on fat-tailed distributions, unknown unknowns, fragility, and the non-linear impact of rare events. You know that most forecasters underestimate tail probabilities and that the expected value of tails dominates many decisions."),
    ("Momentum Analysts", "You are 64 momentum analysts. You track trend persistence, autocorrelation, and the tendency of winners to keep winning and losers to keep losing over medium timeframes. You model momentum across multiple domains and know that momentum works until it doesn't — reversals are violent."),
    ("Mean Reversion Traders", "You are 64 mean reversion specialists. You identify overreactions, stretched valuations, and temporary dislocations that will correct. You model the speed and completeness of reversion. You know that mean reversion and momentum coexist at different timeframes."),
    ("Seasonal Pattern Analysts", "You are 64 seasonal and calendar effect analysts. You track day-of-week, month-of-year, holiday, and event-driven seasonal patterns. You model how recurring temporal factors affect outcomes and prices. You know that well-known seasonal patterns get arbitraged but new ones emerge."),
    ("Structural Change Analysts", "You are 64 structural break and regime change analysts. You identify when the underlying data-generating process shifts permanently. You distinguish temporary deviations from structural changes. You use CUSUM, Chow tests, and Hidden Markov Models. You know that the most costly mistakes come from assuming stationarity."),

    # Category 6: Adversarial (8 groups)
    ("Devil's Advocates", "You are 64 devil's advocates tasked with arguing AGAINST the consensus. Your job is to find the strongest possible case for the opposite view. You steel-man the contrarian position and identify the specific conditions under which the consensus would be catastrophically wrong. You are intellectually honest — you argue against even views you personally hold."),
    ("Professional Contrarians", "You are 64 professional contrarians who have profited from going against the crowd. You track consensus positioning and look for overcrowded trades. You know that the crowd is right most of the time but wrong at the extremes — and the extremes are where the money is."),
    ("Bear Case Specialists", "You are 64 bear case specialists. You construct the most rigorous negative scenario: what has to go wrong, what risks are underpriced, what catalysts could trigger a decline. You quantify downside scenarios with specific probability weights."),
    ("Crash & Crisis Analysts", "You are 64 crash and crisis analysts. You study historical crashes, panics, and cascading failures. You identify systemic vulnerabilities, leverage buildup, and contagion channels. You know that crashes don't happen randomly — they happen when multiple risk factors converge."),
    ("Bubble Detection Team", "You are 64 bubble detection specialists. You use metrics like price-to-fundamentals ratios, speculation indices, margin/leverage levels, newcomer participation rates, and narrative detachment from reality. You've studied every bubble from Dutch tulips to crypto 2021."),
    ("Skeptics & Debunkers", "You are 64 professional skeptics. You demand extraordinary evidence for extraordinary claims. You check sources, question methodology, look for motivated reasoning, and identify unfalsifiable claims. You know that most confident predictions are overconfident."),
    ("Risk Assessment Team", "You are 64 quantitative risk assessors from insurance, reinsurance, and sovereign risk agencies. You convert qualitative scenarios into probability-weighted loss distributions. You model correlation, concentration, and cascade risk. You always ask: what's the worst case and how likely is it?"),
    ("Tail Risk Modelers", "You are 64 tail risk specialists. You model extreme events using EVT (Extreme Value Theory), copulas for tail dependence, and scenario analysis for fat-tailed distributions. You know that VaR underestimates tail risk and that CVaR is a better measure. You price in the tails that others ignore."),

    # Category 7: Meta (8 groups)
    ("Crowd Psychology Analysts", "You are 64 crowd psychology experts. You study how groups form beliefs, how information cascades develop, how herding behavior emerges, and when crowds are wise vs mad. You track sentiment indicators, positioning data, and behavioral biases at the aggregate level. You know Galton's wisdom of crowds requires independence."),
    ("Narrative & Framing Analysts", "You are 64 narrative analysts. You track how stories develop, which narratives gain traction, how framing affects probability assessment, and the lifecycle of narratives (emergence, mainstream adoption, consensus, exhaustion). You know that narratives drive prices as much as fundamentals in the short term."),
    ("Media Impact Modelers", "You are 64 media impact analysts. You model how news coverage, editorial decisions, and algorithmic amplification affect public perception and market prices. You track coverage volume, tone, placement, and the lag between media exposure and behavioral response."),
    ("Social Media Sentiment Analysts", "You are 64 social media sentiment analysts. You process Twitter/X, Reddit, TikTok, YouTube, and niche forums. You use NLP, engagement metrics, and virality indicators. You distinguish organic sentiment from astroturfing and know that social media amplifies extremes."),
    ("Prediction Market Calibration Team", "You are 64 prediction market calibration specialists. You study the empirical accuracy of prediction markets: when they're well-calibrated, when they're biased, and what structural factors affect their efficiency. You know that prediction markets are the best forecasters on average but have systematic biases at the extremes."),
    ("Bayesian Updating Specialists", "You are 64 Bayesian updating specialists. You assess how new information should rationally update prior beliefs. You model the likelihood ratio of each piece of evidence and identify when markets are under- or over-reacting to news. You enforce coherent probability updates."),
    ("Information Cascade Analysts", "You are 64 information cascade researchers. You study how sequential decision-making can lead to rational herding and fragile consensus. You identify when markets are in a cascade (everyone following everyone else) vs reflecting genuine independent information. You know that cascades break suddenly."),
    ("Herding Behavior Detectors", "You are 64 herding behavior specialists. You track position clustering, copycat strategies, benchmark-hugging, and the dynamics of institutional herding. You identify when market prices reflect independent assessment vs coordinated movement, and when herding creates fragility."),

    # Category 8: Wildcard (8 groups)
    ("Cross-Domain Analogy Experts", "You are 64 cross-domain analogy experts. You find structural parallels between seemingly unrelated domains — how weather patterns map to market dynamics, how evolutionary biology explains competitive strategy, how epidemiology models information spread. Your unexpected connections often reveal hidden dynamics."),
    ("Chaos & Complexity Theorists", "You are 64 complexity scientists. You model emergent behavior, tipping points, phase transitions, self-organized criticality, and sensitive dependence on initial conditions. You know that complex systems produce surprises and that deterministic models fail at bifurcation points."),
    ("Game Theory Strategists", "You are 64 game theorists. You model strategic interactions, Nash equilibria, mechanism design, signaling games, and evolutionary game theory. You ask: who are the players, what are their payoffs, and what's the equilibrium? You know that most situations involve imperfect information and repeated games."),
    ("Behavioral Economics Analysts", "You are 64 behavioral economists. You catalog and exploit cognitive biases: anchoring, availability heuristic, loss aversion, status quo bias, framing effects, and the overconfidence gap. You know that markets are not fully rational and that biases create systematic mispricings."),
    ("Political Economy Analysts", "You are 64 political economy analysts. You model the intersection of politics and economics: regulatory capture, rent-seeking, public choice theory, and the political business cycle. You understand that economic outcomes are often determined by political decisions, not market forces."),
    ("Geopolitical Risk Modelers", "You are 64 geopolitical risk quantifiers. You convert geopolitical scenarios into probabilistic risk models. You track conflict indicators, alliance reliability, deterrence credibility, and escalation dynamics. You build scenario trees with conditional probabilities."),
    ("Black Box ML Ensemble", "You are 64 black-box ML models (simulated). You represent the aggregate output of transformer models, LSTMs, and deep learning systems trained on historical prediction market data. You capture nonlinear patterns that human analysts miss but sometimes overfit to noise. You provide a complementary signal."),
    ("Quantum Random & Anti-Consensus", "You are 64 anti-consensus randomizers. You deliberately introduce noise and randomness into the prediction process to break groupthink. You sample from extreme distributions, consider unlikely scenarios, and ensure that the final consensus hasn't converged prematurely. You are the immune system against intellectual monoculture."),
]


def _build_64_groups(domain: str) -> list[tuple[str, str]]:
    """Build the full 64-group roster: 56 universal + 8 domain-specific."""
    domain_groups = _DOMAIN_SPECIFIC_GROUPS.get(domain, _DOMAIN_SPECIFIC_GROUPS["politics"])
    return _UNIVERSAL_GROUPS + domain_groups


def _extract_prob_and_confidence(text: str) -> tuple[Optional[float], Optional[float], str]:
    """Extract probability, confidence, and reasoning from an LLM response."""
    prob = _extract_probability(text)

    conf = None
    conf_match = re.search(r'CONFIDENCE:\s*(0\.\d+)', text, re.IGNORECASE)
    if conf_match:
        conf = float(conf_match.group(1))

    # Extract first meaningful sentence as reasoning
    reasoning = ""
    for line in text.split("\n"):
        line = line.strip()
        if len(line) > 20 and not line.startswith("ESTIMATE") and not line.startswith("PROBABILITY") and not line.startswith("CONFIDENCE"):
            reasoning = line[:200]
            break

    return prob, conf, reasoning


def _run_layer1(market: DomainMarket, context: str, domain: str) -> list[dict]:
    """
    Layer 1 — Independent Analysis: 64 groups, each simulating 64 analysts.
    Temperature: 0.8 (high diversity).
    Returns list of {name, probability, confidence, reasoning}.
    → 64 LLM calls
    """
    groups = _build_64_groups(domain)
    domain_label = DOMAIN_LABELS.get(domain, domain)
    results = []

    for group_name, group_persona in groups:
        system_prompt = (
            f"{group_persona}\n\n"
            f"You are predicting a {domain_label} outcome for a prediction market.\n"
            f"Your panel of 64 specialists must debate internally and reach consensus.\n\n"
            f"IMPORTANT: End your response with exactly:\n"
            f"ESTIMATE: 0.XX\n"
            f"CONFIDENCE: 0.XX"
        )

        user_prompt = (
            f"Market question: {market.question}\n"
            f"Outcomes: {' vs '.join(market.outcomes)}\n\n"
            f"{context}\n\n"
            f"As {group_name}, debate internally among your 64 specialists.\n"
            f"Consider dissenting views within your panel. What's the probability of the FIRST outcome?\n"
            f"Give your key reasoning in 1 sentence, then:\n"
            f"ESTIMATE: 0.XX\n"
            f"CONFIDENCE: 0.XX (how sure your panel is, 0.0-1.0)"
        )

        response = _call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=300,
        )

        if response:
            prob, conf, reasoning = _extract_prob_and_confidence(response)
            if prob is not None:
                results.append({
                    "name": group_name,
                    "probability": prob,
                    "confidence": conf or 0.5,
                    "reasoning": reasoning,
                })

        time.sleep(0.1)  # Rate limiting

    return results


def _run_layer2(market: DomainMarket, l1_results: list[dict], domain: str) -> list[dict]:
    """
    Layer 2 — Panel Debates: 16 panels of 4 groups each.
    Groups that were far from the mean get challenged.
    Temperature: 0.6 (moderate convergence).
    → 16 LLM calls
    """
    if len(l1_results) < 4:
        return l1_results

    # Divide into 16 panels of ~4 groups
    panels = []
    for i in range(0, len(l1_results), 4):
        panel = l1_results[i:i + 4]
        if panel:
            panels.append(panel)

    # If odd division, merge last small panel into previous
    if len(panels) > 1 and len(panels[-1]) < 2:
        panels[-2].extend(panels[-1])
        panels.pop()

    domain_label = DOMAIN_LABELS.get(domain, domain)
    results = []
    overall_mean = sum(r["probability"] for r in l1_results) / len(l1_results)

    for panel_idx, panel in enumerate(panels):
        panel_text = ""
        for g in panel:
            deviation = g["probability"] - overall_mean
            flag = " [OUTLIER — challenged by other groups]" if abs(deviation) > 0.15 else ""
            panel_text += (
                f"- **{g['name']}**: {g['probability']:.3f} (conf: {g['confidence']:.2f}){flag}\n"
                f"  Reasoning: {g['reasoning'][:150]}\n"
            )

        panel_mean = sum(g["probability"] for g in panel) / len(panel)
        panel_std = (sum((g["probability"] - panel_mean) ** 2 for g in panel) / len(panel)) ** 0.5

        system_prompt = (
            f"You are moderating a debate panel of {len(panel)} analyst groups on a {domain_label} question.\n"
            f"The overall mean from all 64 groups was {overall_mean:.3f}.\n"
            f"Your panel's mean is {panel_mean:.3f} (std: {panel_std:.3f}).\n\n"
            f"Simulate the debate: which arguments won? Did outliers get convinced or hold firm?\n"
            f"Groups far from the overall mean should be challenged — but they might be right.\n\n"
            f"End with:\n"
            f"CONSENSUS: 0.XX\n"
            f"CONFIDENCE: 0.XX"
        )

        user_prompt = (
            f"Market: {market.question}\n"
            f"Outcomes: {' vs '.join(market.outcomes)}\n\n"
            f"## Panel {panel_idx + 1} Members:\n{panel_text}\n"
            f"Simulate the debate. Which arguments were strongest? "
            f"What's the revised panel consensus after debate?\n\n"
            f"End with:\nCONSENSUS: 0.XX\nCONFIDENCE: 0.XX"
        )

        response = _call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.6,
            max_tokens=400,
        )

        if response:
            prob = _extract_probability(response)
            conf_match = re.search(r'CONFIDENCE:\s*(0\.\d+)', response, re.IGNORECASE)
            conf = float(conf_match.group(1)) if conf_match else 0.5
            reasoning = ""
            for line in response.split("\n"):
                line = line.strip()
                if len(line) > 20 and not line.startswith("CONSENSUS") and not line.startswith("CONFIDENCE"):
                    reasoning = line[:200]
                    break

            if prob is not None:
                results.append({
                    "panel_idx": panel_idx,
                    "probability": prob,
                    "confidence": conf,
                    "reasoning": reasoning,
                    "member_count": len(panel),
                    "pre_debate_mean": panel_mean,
                    "pre_debate_std": panel_std,
                })

        time.sleep(0.1)

    return results


def _run_layer3(market: DomainMarket, l2_results: list[dict], domain: str) -> list[dict]:
    """
    Layer 3 — Summit Synthesis: 4 summits of 4 panels each.
    "Panels presented findings at a summit. The audience voted."
    Temperature: 0.4 (convergence tightens).
    → 4 LLM calls
    """
    if len(l2_results) < 2:
        return l2_results

    # Divide into 4 summits of ~4 panels
    summits = []
    per_summit = max(1, len(l2_results) // 4)
    for i in range(0, len(l2_results), per_summit):
        s = l2_results[i:i + per_summit]
        if s:
            summits.append(s)

    # Merge tiny last summit
    if len(summits) > 1 and len(summits[-1]) < 2:
        summits[-2].extend(summits[-1])
        summits.pop()

    domain_label = DOMAIN_LABELS.get(domain, domain)
    overall_l2_mean = sum(r["probability"] for r in l2_results) / len(l2_results)
    results = []

    for summit_idx, summit_panels in enumerate(summits):
        panel_text = ""
        for p in summit_panels:
            shift = p["probability"] - p.get("pre_debate_mean", p["probability"])
            shift_str = f" (shifted {shift:+.3f} during debate)" if abs(shift) > 0.01 else ""
            panel_text += (
                f"- Panel {p.get('panel_idx', '?')}: {p['probability']:.3f} "
                f"(conf: {p['confidence']:.2f}, {p.get('member_count', 4)} groups){shift_str}\n"
                f"  Key finding: {p['reasoning'][:150]}\n"
            )

        summit_mean = sum(p["probability"] for p in summit_panels) / len(summit_panels)

        system_prompt = (
            f"You are chairing a summit on a {domain_label} prediction.\n"
            f"{len(summit_panels)} panels presented their findings. Overall mean across all panels: {overall_l2_mean:.3f}.\n"
            f"This summit's panels average: {summit_mean:.3f}.\n\n"
            f"Synthesize: what emerged as the dominant view? Were there minority reports?\n"
            f"Weight by confidence and the quality of reasoning.\n\n"
            f"End with:\nPROBABILITY: 0.XX\nCONFIDENCE: 0.XX"
        )

        user_prompt = (
            f"Market: {market.question}\n"
            f"Outcomes: {' vs '.join(market.outcomes)}\n\n"
            f"## Summit {summit_idx + 1} Panels:\n{panel_text}\n"
            f"What's the summit consensus? Any strong minority reports?\n\n"
            f"End with:\nPROBABILITY: 0.XX\nCONFIDENCE: 0.XX"
        )

        response = _call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=400,
        )

        if response:
            prob = _extract_probability(response)
            conf_match = re.search(r'CONFIDENCE:\s*(0\.\d+)', response, re.IGNORECASE)
            conf = float(conf_match.group(1)) if conf_match else 0.5
            reasoning = ""
            for line in response.split("\n"):
                line = line.strip()
                if len(line) > 20 and not any(line.startswith(kw) for kw in ["PROBABILITY", "CONFIDENCE"]):
                    reasoning = line[:200]
                    break

            if prob is not None:
                results.append({
                    "summit_idx": summit_idx,
                    "probability": prob,
                    "confidence": conf,
                    "reasoning": reasoning,
                    "panel_count": len(summit_panels),
                })

        time.sleep(0.1)

    return results


def _run_layer4(market: DomainMarket, l3_results: list[dict], domain: str,
                convergence_data: dict) -> tuple[float, float, str, str, str, str]:
    """
    Layer 4 — Final Consensus: 1 call synthesizing all summit outputs.
    Temperature: 0.2 (maximum convergence).
    Returns: (probability, confidence, key_reasoning, bull_case, bear_case, uncertainty)
    → 1 LLM call
    """
    if not l3_results:
        return 0.5, 0.0, "No summit results", "", "", ""

    domain_label = DOMAIN_LABELS.get(domain, domain)

    summit_text = ""
    for s in l3_results:
        summit_text += (
            f"- Summit {s.get('summit_idx', '?')}: {s['probability']:.3f} "
            f"(conf: {s['confidence']:.2f}, {s.get('panel_count', 4)} panels)\n"
            f"  Key finding: {s['reasoning'][:200]}\n"
        )

    conv = convergence_data
    convergence_text = (
        f"## Convergence Tracking:\n"
        f"- Layer 1 (64 groups): mean={conv.get('l1_mean', 0):.3f}, std={conv.get('l1_std', 0):.3f}\n"
        f"- Layer 2 (16 panels): mean={conv.get('l2_mean', 0):.3f}, std={conv.get('l2_std', 0):.3f}\n"
        f"- Layer 3 (4 summits): mean={conv.get('l3_mean', 0):.3f}, std={conv.get('l3_std', 0):.3f}\n"
        f"- Convergence: std went {conv.get('l1_std', 0):.3f} → {conv.get('l2_std', 0):.3f} → {conv.get('l3_std', 0):.3f}\n"
    )

    system_prompt = (
        f"You are the final arbiter synthesizing a {domain_label} prediction from 4096 AI agents.\n"
        f"These agents were organized hierarchically:\n"
        f"  Layer 1: 64 specialist groups of 64 analysts each (4096 total)\n"
        f"  Layer 2: Debated in 16 panels\n"
        f"  Layer 3: Synthesized at 4 summits\n"
        f"  Layer 4: You — final consensus.\n\n"
        f"Weight each summit by its confidence. If summits agree closely, trust the consensus.\n"
        f"If they diverge, investigate why and report the uncertainty.\n\n"
        f"End with exactly:\n"
        f"PROBABILITY: 0.XX\n"
        f"CONFIDENCE: 0.XX\n"
        f"BULL: (1 sentence strongest case FOR)\n"
        f"BEAR: (1 sentence strongest case AGAINST)\n"
        f"UNCERTAINTY: (1 sentence biggest unknown)"
    )

    user_prompt = (
        f"Market: {market.question}\n"
        f"Outcomes: {' vs '.join(market.outcomes)}\n\n"
        f"## Summit Results:\n{summit_text}\n"
        f"{convergence_text}\n"
        f"Synthesize the final probability from 4096 agents. Report the consensus.\n\n"
        f"End with:\n"
        f"PROBABILITY: 0.XX\n"
        f"CONFIDENCE: 0.XX\n"
        f"BULL: ...\n"
        f"BEAR: ...\n"
        f"UNCERTAINTY: ..."
    )

    response = _call_llm(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=600,
    )

    if not response:
        # Fallback: confidence-weighted mean of summits
        total_w = sum(s["confidence"] for s in l3_results)
        if total_w > 0:
            prob = sum(s["probability"] * s["confidence"] for s in l3_results) / total_w
        else:
            prob = sum(s["probability"] for s in l3_results) / len(l3_results)
        return prob, 0.4, "Fallback: weighted summit mean", "", "", ""

    prob = _extract_probability(response)
    if prob is None:
        total_w = sum(s["confidence"] for s in l3_results)
        prob = sum(s["probability"] * s["confidence"] for s in l3_results) / total_w if total_w > 0 else 0.5

    conf_match = re.search(r'CONFIDENCE:\s*(0\.\d+)', response, re.IGNORECASE)
    confidence = float(conf_match.group(1)) if conf_match else 0.5

    bull_match = re.search(r'BULL:\s*(.+)', response, re.IGNORECASE)
    bull_case = bull_match.group(1).strip()[:200] if bull_match else ""

    bear_match = re.search(r'BEAR:\s*(.+)', response, re.IGNORECASE)
    bear_case = bear_match.group(1).strip()[:200] if bear_match else ""

    unc_match = re.search(r'UNCERTAINTY:\s*(.+)', response, re.IGNORECASE)
    uncertainty = unc_match.group(1).strip()[:200] if unc_match else ""

    # Key reasoning from main body
    reasoning_lines = []
    for line in response.split("\n"):
        line = line.strip()
        if (len(line) > 20 and
                not any(line.upper().startswith(kw) for kw in
                        ["PROBABILITY", "CONFIDENCE", "BULL", "BEAR", "UNCERTAINTY"])):
            reasoning_lines.append(line[:150])
            if len(reasoning_lines) >= 2:
                break
    key_reasoning = " | ".join(reasoning_lines) if reasoning_lines else response[:300]

    return prob, confidence, key_reasoning, bull_case, bear_case, uncertainty


def run_hierarchical_4096(market: DomainMarket, context: str,
                          domain: str = None) -> Optional[CrowdSignal]:
    """
    Run the 4-layer hierarchical 4096-agent Delphi simulation.

    Architecture:
      Layer 1: 64 groups x 64 agents = 4096 (independent analysis) → 64 calls
      Layer 2: 16 panels of 4 groups (debate) → 16 calls
      Layer 3: 4 summits of 4 panels (synthesis) → 4 calls
      Layer 4: 1 final consensus → 1 call
      Total: 85 calls per market ≈ $0.085

    Args:
        market: DomainMarket to analyze
        context: enrichment context string
        domain: override domain (default: market.domain)

    Returns:
        CrowdSignal with full hierarchy metadata
    """
    domain = domain or market.domain
    if not domain:
        logger.warning("[CROWD-4096] No domain specified")
        return None

    logger.info(
        f"[CROWD-4096] Starting hierarchical simulation: "
        f"{market.question[:60]}... (domain={domain})"
    )
    t_start = time.time()

    # ── Layer 1: 64 groups, independent analysis ──
    t1_start = time.time()
    l1_results = _run_layer1(market, context, domain)
    t1_end = time.time()

    if len(l1_results) < 10:
        logger.warning(
            f"[CROWD-4096] L1 too few groups ({len(l1_results)}/64), aborting"
        )
        return None

    l1_probs = [r["probability"] for r in l1_results]
    l1_mean = sum(l1_probs) / len(l1_probs)
    l1_std = (sum((p - l1_mean) ** 2 for p in l1_probs) / len(l1_probs)) ** 0.5
    logger.info(
        f"[CROWD-4096] L1 ({t1_end - t1_start:.0f}s): {len(l1_results)} groups, "
        f"mean={l1_mean:.3f}, std={l1_std:.3f}, "
        f"range=[{min(l1_probs):.3f}, {max(l1_probs):.3f}]"
    )

    # ── Layer 2: 16 panels, debate ──
    t2_start = time.time()
    l2_results = _run_layer2(market, l1_results, domain)
    t2_end = time.time()

    if len(l2_results) < 2:
        logger.warning(f"[CROWD-4096] L2 too few panels ({len(l2_results)}), using L1 fallback")
        # Fallback: group L1 into pseudo-panels
        l2_results = [{
            "panel_idx": 0,
            "probability": l1_mean,
            "confidence": 0.5,
            "reasoning": "L2 fallback to L1 mean",
            "member_count": len(l1_results),
            "pre_debate_mean": l1_mean,
            "pre_debate_std": l1_std,
        }]

    l2_probs = [r["probability"] for r in l2_results]
    l2_mean = sum(l2_probs) / len(l2_probs)
    l2_std = (sum((p - l2_mean) ** 2 for p in l2_probs) / len(l2_probs)) ** 0.5
    logger.info(
        f"[CROWD-4096] L2 ({t2_end - t2_start:.0f}s): {len(l2_results)} panels, "
        f"mean={l2_mean:.3f}, std={l2_std:.3f}"
    )

    # ── Layer 3: 4 summits ──
    t3_start = time.time()
    l3_results = _run_layer3(market, l2_results, domain)
    t3_end = time.time()

    if not l3_results:
        logger.warning("[CROWD-4096] L3 failed, using L2 fallback")
        l3_results = [{
            "summit_idx": 0,
            "probability": l2_mean,
            "confidence": 0.5,
            "reasoning": "L3 fallback to L2 mean",
            "panel_count": len(l2_results),
        }]

    l3_probs = [r["probability"] for r in l3_results]
    l3_mean = sum(l3_probs) / len(l3_probs)
    l3_std = (sum((p - l3_mean) ** 2 for p in l3_probs) / len(l3_probs)) ** 0.5
    logger.info(
        f"[CROWD-4096] L3 ({t3_end - t3_start:.0f}s): {len(l3_results)} summits, "
        f"mean={l3_mean:.3f}, std={l3_std:.3f}"
    )

    # ── Layer 4: Final consensus ──
    convergence_data = {
        "l1_mean": l1_mean, "l1_std": l1_std,
        "l2_mean": l2_mean, "l2_std": l2_std,
        "l3_mean": l3_mean, "l3_std": l3_std,
    }

    t4_start = time.time()
    final_p, confidence, key_reasoning, bull_case, bear_case, uncertainty = _run_layer4(
        market, l3_results, domain, convergence_data
    )
    t4_end = time.time()

    total_time = t4_end - t_start
    total_calls = len(l1_results) + len(l2_results) + len(l3_results) + 1
    est_cost = total_calls * 0.001

    logger.info(
        f"[CROWD-4096] L4 ({t4_end - t4_start:.0f}s): final={final_p:.3f}, "
        f"confidence={confidence:.2f}"
    )
    logger.info(
        f"[CROWD-4096] L1: {len(l1_results)} groups -> "
        f"L2: {len(l2_results)} panels -> "
        f"L3: {len(l3_results)} summits -> "
        f"L4: consensus={final_p:.3f} | "
        f"{total_calls} calls, ~${est_cost:.3f}, {total_time:.0f}s"
    )
    logger.info(
        f"[CROWD-4096] Convergence: std {l1_std:.3f} -> {l2_std:.3f} -> {l3_std:.3f} "
        f"({'converged' if l3_std < l1_std else 'DIVERGED'})"
    )
    if bull_case:
        logger.info(f"[CROWD-4096] BULL: {bull_case[:100]}")
    if bear_case:
        logger.info(f"[CROWD-4096] BEAR: {bear_case[:100]}")
    if uncertainty:
        logger.info(f"[CROWD-4096] UNCERTAINTY: {uncertainty[:100]}")

    # ── Signal generation (same logic as original) ──
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

    # Enriched reasoning with hierarchy metadata
    full_reasoning = (
        f"{key_reasoning} | "
        f"4096-agent hierarchy: L1 std={l1_std:.3f}, L2 std={l2_std:.3f}, L3 std={l3_std:.3f} | "
        f"Bull: {bull_case[:80]} | Bear: {bear_case[:80]}"
    )

    logger.info(
        f"[CROWD-4096] [{domain.upper()}] COMPLETE ({total_time:.0f}s, {total_calls} calls, ~${est_cost:.3f}): "
        f"{market.question[:50]} | crowd={final_p:.3f} vs PM={yes_price:.3f} | "
        f"edge={edge:.3f} ({edge * 100:.1f}%) | {side} ${kelly_size:.0f} | "
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
        key_reasoning=full_reasoning[:500],
        round1_estimates=l1_probs,
        round2_estimates=[r["probability"] for r in l2_results],
        round3_final=final_p,
        std_dev=round(l3_std, 4),
        timestamp=datetime.now(timezone.utc).isoformat(),
        token_id=token_id,
        domain=domain,
    )


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

        # v12.8: Filter out longshots and near-certainties
        markets = [m for m in markets
                   if m.outcome_prices and 0.10 <= m.outcome_prices[0] <= 0.80]

        # v12.8: Filter markets where crowd has no informational edge
        pre_filter = len(markets)
        markets = [m for m in markets
                   if not any(kw in m.question.lower() for kw in CROWD_BLIND_KEYWORDS)]
        if pre_filter != len(markets):
            logger.info(f"[CROWD-PRED] Filtered {pre_filter - len(markets)} crowd-blind markets")

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

            # v12.8: Deep research pre-filter (MiroThinker-inspired)
            # Research first, simulate only if research says crowd has edge
            try:
                from utils.deep_researcher import research_market
                yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5
                report = research_market(market.question, yes_price, domain)
                if report:
                    if report.recommendation == "SKIP":
                        logger.info(
                            f"[CROWD-PRED] [{domain.upper()}] [{i+1}/{len(markets)}] "
                            f"RESEARCH SKIP: {market.question[:50]} | "
                            f"quality={report.information_quality} crowd_edge={report.crowd_has_edge}"
                        )
                        continue
                    elif report.recommendation == "TRADE_DIRECTLY":
                        # Research alone is sufficient — create signal without simulation
                        edge = report.probability - yes_price if report.probability > yes_price else (1 - report.probability) - (1 - yes_price)
                        if abs(edge) >= self.MIN_EDGE and report.confidence >= 0.65:
                            side = "BUY_YES" if report.probability > yes_price else "BUY_NO"
                            signal = CrowdSignal(
                                market=market,
                                question=market.question,
                                crowd_probability=report.probability,
                                polymarket_price=yes_price,
                                edge=abs(edge),
                                side=side,
                                confidence=report.confidence,
                                std_dev=0.05,
                                kelly_size=min(self.MAX_BET, max(5, abs(edge) * self.KELLY_FRACTION * 1000)),
                                reasoning=f"[RESEARCH-DIRECT] {report.bull_case} | {report.bear_case}",
                            )
                            signals.append(signal)
                            _save_cached_signal(signal, domain)
                            logger.info(
                                f"[CROWD-PRED] [{domain.upper()}] [{i+1}/{len(markets)}] "
                                f"RESEARCH DIRECT: {side} edge={abs(edge):.1%} | {market.question[:50]}"
                            )
                            continue
                    # else: SIMULATE — proceed with crowd simulation
                    logger.info(
                        f"[CROWD-PRED] [{domain.upper()}] [{i+1}/{len(markets)}] "
                        f"RESEARCH → SIMULATE: {market.question[:50]} | "
                        f"quality={report.information_quality}"
                    )
            except Exception as e:
                logger.debug(f"[RESEARCH] Pre-filter error: {e}")

            # Enrich with domain-specific context
            mode = "4096-agent hierarchical" if AGENT_COUNT >= 4096 else "50-agent Delphi"
            logger.info(
                f"[CROWD-PRED] [{domain.upper()}] [{i+1}/{len(markets)}] "
                f"Simulating ({mode}): {market.question[:60]}"
            )
            context = build_enrichment_context(market)

            # Run Delphi — hierarchical 4096 or fast 50
            if AGENT_COUNT >= 4096:
                signal = run_hierarchical_4096(market, context, domain)
            else:
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
    parser.add_argument("--agents", type=int, default=None,
                        choices=[50, 4096], help="Agent count: 50 (fast) or 4096 (hierarchical)")
    args = parser.parse_args()

    # Override AGENT_COUNT from CLI
    if args.agents is not None:
        global AGENT_COUNT
        AGENT_COUNT = args.agents
        logger.info(f"[CROWD-PRED] Agent count set to {AGENT_COUNT}")

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
