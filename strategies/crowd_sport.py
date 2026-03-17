"""
Crowd Sport Prediction Strategy v1.0 — Multi-Agent Delphi Simulation
=====================================================================

Uses Claude Opus via LiteLLM proxy to simulate a crowd of 50 diverse sport
analysts across 10 specialist groups. Three-round Delphi method:
  Round 1: Independent estimates from 10 groups (5 analysts each)
  Round 2: Groups see other groups' estimates, can revise (information cascade)
  Round 3: Final weighted consensus with confidence intervals

Edge detection: compare crowd probability vs Polymarket price.
Sizing: quarter-Kelly with conservative fraction (0.15).

Fee-free on sport markets (feesEnabled=false on Polymarket).

Cost: ~$0.15-0.30 per simulation (Opus via LiteLLM proxy).
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
LITELLM_MODEL = "anthropic/claude-opus-4-20250514"

GAMMA_EVENTS_API = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_API = "https://gamma-api.polymarket.com/markets"

# ESPN free endpoints
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
ESPN_STANDINGS = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/standings"
ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{team_id}"

CACHE_DIR = Path("logs/crowd_sport_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_FILE = Path("logs/crowd_sport_predictions.json")
CACHE_TTL = 6 * 3600  # 6 hours — don't re-simulate same market

# Strategy parameters
MIN_EDGE = 0.05          # 5% minimum crowd vs market disagreement
MAX_BET = 50.0           # conservative start
KELLY_FRACTION = 0.15    # quarter-Kelly
MIN_VOLUME = 10_000      # $10K minimum market volume
MAX_MARKETS_PER_SCAN = 10  # limit API costs
TEMPERATURE = 0.7        # diversity in agent responses

SPORT_KEYWORDS = [
    "nba", "nfl", "nhl", "mlb", "ipl", "premier league",
    "champions league", "la liga", "bundesliga", "serie a",
    "ligue 1", "copa", "stanley cup", "super bowl",
    "world series", "ufc", "boxing", "tennis", "wimbledon",
    "grand slam", "grand prix", "formula 1", "f1",
    "cricket", "rugby", "ncaa", "march madness",
    "playoffs", "finals", "world cup", "olympic",
    "win the", "beat", "defeat",
    # Major teams
    "lakers", "celtics", "warriors", "knicks", "bulls", "bucks",
    "nuggets", "suns", "76ers", "heat", "nets", "clippers",
    "yankees", "dodgers", "chiefs", "eagles", "49ers",
    "manchester", "liverpool", "barcelona", "real madrid",
    "arsenal", "chelsea", "bayern", "juventus", "inter",
    "psg", "napoli", "ac milan", "tottenham", "spurs",
]

# ESPN sport/league mapping for enrichment
ESPN_LEAGUES = {
    "nba": ("basketball", "nba"),
    "nfl": ("football", "nfl"),
    "nhl": ("hockey", "nhl"),
    "mlb": ("baseball", "mlb"),
    "premier league": ("soccer", "eng.1"),
    "champions league": ("soccer", "uefa.champions"),
    "la liga": ("soccer", "esp.1"),
    "bundesliga": ("soccer", "ger.1"),
    "serie a": ("soccer", "ita.1"),
    "ligue 1": ("soccer", "fra.1"),
    "ncaa": ("basketball", "mens-college-basketball"),
    "ufc": ("mma", "ufc"),
}

# ── Analyst Personas ───────────────────────────────────────────────

ANALYST_GROUPS = [
    {
        "name": "Statistical/Sabermetric",
        "role": "You are a quantitative sports analyst who relies heavily on advanced statistics. "
                "For basketball: ELO ratings, win shares, PER, true shooting %, net rating, SRS. "
                "For soccer: xG (expected goals), progressive passes, pressing intensity, PPDA. "
                "For other sports: equivalent advanced metrics. "
                "You distrust narratives and eye tests. Only numbers matter. "
                "You build regression models and trust base rates over anecdotes.",
    },
    {
        "name": "Scout/Eye Test",
        "role": "You are a veteran scout who watches every game. You evaluate roster talent, "
                "coaching quality, tactical matchups, and the 'eye test' — intangibles that "
                "stats miss. You know which players elevate in big moments, which coaches "
                "make better adjustments, and which teams have the X-factor. "
                "You're skeptical of pure stat models because context matters.",
    },
    {
        "name": "Sharp Money/Market",
        "role": "You are a professional sports bettor who tracks line movements, whale activity, "
                "and closing line value. You know where the sharp money is flowing, which books "
                "moved first (Pinnacle, Circa), and whether the public is on the wrong side. "
                "You pay attention to reverse line movement and steam moves. "
                "Market efficiency is your bible, but you know where edges exist.",
    },
    {
        "name": "Contrarian/Insider",
        "role": "You are a contrarian analyst with insider-level knowledge. You focus on factors "
                "the market underweights: locker room dynamics, trade rumors, undisclosed injuries, "
                "coaching tensions, schedule fatigue, travel, altitude effects, referee assignments. "
                "You always look for the overlooked angle that could flip the outcome.",
    },
    {
        "name": "Historical Pattern",
        "role": "You are a historian of sports who sees everything through the lens of precedent. "
                "Dynasty cycles (rise/peak/decline), championship windows, draft class comparisons, "
                "regression to the mean patterns, and 'never bet against X in Y situation' rules. "
                "You track how teams perform after specific events (trade deadlines, injuries to "
                "stars, coaching changes) based on decades of data.",
    },
    {
        "name": "Momentum/Form",
        "role": "You are a form analyst who focuses on recent performance: last 5-10 games, "
                "hot/cold streaks, schedule difficulty, rest days, back-to-back fatigue, "
                "and momentum shifts. You believe the most recent data is the most predictive. "
                "A team's trajectory matters more than season averages. "
                "You track net rating over rolling windows, not full-season stats.",
    },
    {
        "name": "Defense Specialist",
        "role": "You are a defensive specialist. You evaluate defensive rating, opponent shooting "
                "percentages (at the rim, midrange, 3pt), rim protection, defensive rebounding, "
                "forced turnovers, and transition defense. For soccer: clean sheets, goals conceded "
                "per xG, pressing success rate. You believe defense wins championships "
                "and underrated defensive improvements create market edges.",
    },
    {
        "name": "Offense Specialist",
        "role": "You are an offensive specialist. You evaluate offensive efficiency, pace, "
                "3-point shooting volume and accuracy, transition scoring, half-court offense "
                "quality, star player usage and efficiency. For soccer: goals per xG conversion, "
                "chance creation, set piece effectiveness. You believe elite offense creates "
                "mismatches that defense can't contain in critical moments.",
    },
    {
        "name": "Matchup Specialist",
        "role": "You are a matchup specialist. You focus on head-to-head records, stylistic "
                "matchups (how team A's strengths interact with team B's weaknesses), positional "
                "advantages, coaching schematic matchups, and playoff series dynamics. "
                "You know that general power rankings don't capture specific matchup edges. "
                "A team can be mediocre overall but dominant against a specific opponent's style.",
    },
    {
        "name": "Meta/Narrative",
        "role": "You are a meta-analyst who studies the betting market itself. You track public "
                "betting percentages, media narrative bias, recency bias, and overreaction patterns. "
                "You know when the market is pricing in hype vs reality. "
                "You spot when a team is 'the public's darling' and overbet, or when a legitimate "
                "contender is getting faded because of a bad narrative. "
                "You fade hype and buy overlooked value.",
    },
]

assert len(ANALYST_GROUPS) == 10, "Must have exactly 10 analyst groups"


# ── Data Classes ───────────────────────────────────────────────────

@dataclass
class SportMarket:
    """A sport market from Polymarket."""
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
    key_reasoning: str      # top 3 reasons from crowd
    round1_estimates: list[float]
    round2_estimates: list[float]
    round3_final: float
    std_dev: float          # disagreement measure
    timestamp: str
    token_id: str = ""


# ── ESPN Data Enrichment ───────────────────────────────────────────

def _detect_league(question: str, event_title: str) -> Optional[tuple[str, str]]:
    """Detect which ESPN league to query from question text."""
    text = (question + " " + event_title).lower()
    for keyword, (sport, league) in ESPN_LEAGUES.items():
        if keyword in text:
            return sport, league
    return None


def fetch_espn_data(sport: str, league: str) -> dict:
    """Fetch current scores, standings, and recent results from ESPN free API."""
    data = {"scores": [], "standings": [], "source": "ESPN"}

    # Scoreboard (recent/upcoming games)
    try:
        url = ESPN_SCOREBOARD.format(sport=sport, league=league)
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            scoreboard = resp.json()
            events = scoreboard.get("events", [])
            for event in events[:15]:
                name = event.get("name", "")
                status = event.get("status", {}).get("type", {}).get("shortDetail", "")
                competitors = event.get("competitions", [{}])[0].get("competitors", [])
                scores = []
                for c in competitors:
                    team = c.get("team", {}).get("displayName", "")
                    score = c.get("score", "")
                    record = c.get("records", [{}])[0].get("summary", "") if c.get("records") else ""
                    scores.append(f"{team} {score} ({record})")
                data["scores"].append(f"{name}: {' vs '.join(scores)} — {status}")
    except Exception as e:
        logger.debug(f"[CROWD-SPORT] ESPN scoreboard error: {e}")

    # Standings
    try:
        url = ESPN_STANDINGS.format(sport=sport, league=league)
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            standings = resp.json()
            for group in standings.get("children", []):
                group_name = group.get("name", "")
                entries = group.get("standings", {}).get("entries", [])
                for entry in entries[:10]:
                    team = entry.get("team", {}).get("displayName", "")
                    stats = {s["name"]: s.get("displayValue", s.get("value", ""))
                             for s in entry.get("stats", [])}
                    wins = stats.get("wins", "?")
                    losses = stats.get("losses", "?")
                    pct = stats.get("winPercent", stats.get("gamesBehind", ""))
                    data["standings"].append(f"{group_name}: {team} ({wins}-{losses}, {pct})")
    except Exception as e:
        logger.debug(f"[CROWD-SPORT] ESPN standings error: {e}")

    return data


def build_enrichment_context(market: SportMarket) -> str:
    """Build real-data context to inject into simulation prompts."""
    parts = []

    # Detect league and fetch ESPN data
    league_info = _detect_league(market.question, market.event_title)
    if league_info:
        sport, league = league_info
        espn = fetch_espn_data(sport, league)

        if espn["scores"]:
            parts.append("## Recent/Upcoming Games (ESPN)")
            for s in espn["scores"][:10]:
                parts.append(f"- {s}")

        if espn["standings"]:
            parts.append("\n## Current Standings (ESPN)")
            for s in espn["standings"][:20]:
                parts.append(f"- {s}")

    # Market metadata
    parts.append(f"\n## Market Info")
    parts.append(f"- Question: {market.question}")
    parts.append(f"- Event: {market.event_title}")
    if market.description:
        parts.append(f"- Description: {market.description[:500]}")
    parts.append(f"- Outcomes: {', '.join(market.outcomes)}")
    parts.append(f"- Current prices: {', '.join(f'${p:.3f}' for p in market.outcome_prices)}")
    parts.append(f"- Volume: ${market.volume:,.0f}")
    parts.append(f"- Resolution: {market.end_date}")

    return "\n".join(parts)


# ── LLM Communication ─────────────────────────────────────────────

def _call_llm(messages: list[dict], temperature: float = TEMPERATURE,
              max_tokens: int = 1500) -> Optional[str]:
    """Call Claude Opus via LiteLLM proxy."""
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
            timeout=120,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        else:
            logger.warning(f"[CROWD-SPORT] LLM error {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.warning(f"[CROWD-SPORT] LLM call failed: {e}")
        return None


def _extract_probability(text: str) -> Optional[float]:
    """Extract probability from analyst response."""
    if not text:
        return None

    # Look for PROBABILITY: X.XX or ESTIMATE: X.XX
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

    # Fallback: look for "X%" patterns near probability-like words
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

def _run_round1(market: SportMarket, context: str) -> list[tuple[str, float, str]]:
    """
    Round 1: Independent estimates from 10 groups.
    Each group has a distinct persona. We ask each to estimate independently.
    Returns list of (group_name, probability, reasoning).
    """
    results = []

    for group in ANALYST_GROUPS:
        system_prompt = (
            f"You are part of a group of 5 {group['name']} analysts. "
            f"{group['role']}\n\n"
            f"You are predicting a sports outcome for a prediction market. "
            f"Your group must reach a consensus estimate.\n\n"
            f"IMPORTANT: You must end your response with exactly this format:\n"
            f"ESTIMATE: 0.XX\n"
            f"(a decimal between 0.01 and 0.99 representing the probability of the first outcome)"
        )

        user_prompt = (
            f"As a group of {group['name']} analysts, estimate the probability of the "
            f"FIRST outcome happening.\n\n"
            f"Market question: {market.question}\n"
            f"Outcomes: {' vs '.join(market.outcomes)}\n\n"
            f"{context}\n\n"
            f"Think step by step as a team of 5 {group['name']} specialists. "
            f"Consider all relevant factors from your area of expertise. "
            f"Be specific — cite actual stats, records, or patterns where possible. "
            f"Then agree on a final probability estimate.\n\n"
            f"End with: ESTIMATE: 0.XX"
        )

        response = _call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=TEMPERATURE,
            max_tokens=1200,
        )

        if response:
            prob = _extract_probability(response)
            if prob is not None:
                # Extract brief reasoning (first 2-3 sentences)
                reasoning = response.split("\n")[0][:200]
                results.append((group["name"], prob, reasoning))
                logger.debug(
                    f"[CROWD-SPORT] Round 1 | {group['name']}: {prob:.3f}"
                )
            else:
                logger.debug(f"[CROWD-SPORT] Round 1 | {group['name']}: no probability extracted")
        else:
            logger.debug(f"[CROWD-SPORT] Round 1 | {group['name']}: LLM call failed")

        # Small delay to avoid rate limits
        time.sleep(0.3)

    return results


def _run_round2(
    market: SportMarket,
    context: str,
    round1_results: list[tuple[str, float, str]],
) -> list[tuple[str, float, str]]:
    """
    Round 2: Delphi revision. Each group sees all other groups' estimates.
    Groups can revise their estimate based on peer information.
    Returns list of (group_name, revised_probability, reasoning).
    """
    if not round1_results:
        return []

    # Build summary of Round 1
    r1_summary = "## Round 1 Results from All Analyst Groups:\n"
    for name, prob, reason in round1_results:
        r1_summary += f"- **{name}** analysts: {prob:.3f} ({prob*100:.1f}%) — {reason[:100]}\n"

    mean_r1 = sum(p for _, p, _ in round1_results) / len(round1_results)
    std_r1 = (sum((p - mean_r1)**2 for _, p, _ in round1_results) / len(round1_results)) ** 0.5
    r1_summary += f"\n**Round 1 average: {mean_r1:.3f} (std: {std_r1:.3f})**\n"

    results = []

    for group in ANALYST_GROUPS:
        # Find this group's Round 1 estimate
        own_r1 = None
        for name, prob, _ in round1_results:
            if name == group["name"]:
                own_r1 = prob
                break

        if own_r1 is None:
            continue  # Group didn't produce R1 estimate

        system_prompt = (
            f"You are part of a group of 5 {group['name']} analysts. "
            f"{group['role']}\n\n"
            f"This is Round 2 of a Delphi forecasting exercise. "
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
            f"Now, seeing all groups' estimates, do you want to revise your estimate? "
            f"Consider:\n"
            f"1. Were there factors other groups considered that you missed?\n"
            f"2. Is there information asymmetry — some groups have data you don't?\n"
            f"3. Should you move toward the consensus, or do you have strong reasons to disagree?\n"
            f"4. Beware of anchoring bias — don't just average.\n\n"
            f"Provide your revised estimate (or keep the same if confident).\n"
            f"End with: ESTIMATE: 0.XX"
        )

        response = _call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=TEMPERATURE * 0.8,  # slightly less random in revision
            max_tokens=800,
        )

        if response:
            prob = _extract_probability(response)
            if prob is not None:
                reasoning = response.split("\n")[0][:200]
                results.append((group["name"], prob, reasoning))
                delta = prob - own_r1
                if abs(delta) > 0.02:
                    logger.debug(
                        f"[CROWD-SPORT] Round 2 | {group['name']}: "
                        f"{own_r1:.3f} -> {prob:.3f} (delta {delta:+.3f})"
                    )

        time.sleep(0.3)

    return results


def _run_round3(
    market: SportMarket,
    context: str,
    round1_results: list[tuple[str, float, str]],
    round2_results: list[tuple[str, float, str]],
) -> tuple[float, float, str]:
    """
    Round 3: Final consensus synthesis by a moderator.
    Returns (final_probability, confidence, key_reasoning).
    """
    # Build full Delphi history
    r1_text = "\n".join(
        f"- {name}: {prob:.3f} — {reason[:100]}"
        for name, prob, reason in round1_results
    )
    r2_text = "\n".join(
        f"- {name}: {prob:.3f} — {reason[:100]}"
        for name, prob, reason in round2_results
    )

    # Calculate stats
    r2_probs = [p for _, p, _ in round2_results] if round2_results else [p for _, p, _ in round1_results]
    if not r2_probs:
        return 0.5, 0.0, "No estimates available"

    mean_p = sum(r2_probs) / len(r2_probs)
    std_p = (sum((p - mean_p)**2 for p in r2_probs) / len(r2_probs)) ** 0.5

    # Convergence: did opinions converge from R1 to R2?
    r1_probs = [p for _, p, _ in round1_results]
    r1_std = (sum((p - sum(r1_probs)/len(r1_probs))**2 for p in r1_probs) / len(r1_probs)) ** 0.5 if r1_probs else 1.0

    system_prompt = (
        "You are the moderator of a Delphi forecasting panel. "
        "You have seen two rounds of estimates from 10 specialist groups. "
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
        f"3. If std is low (<0.05), the crowd is confident — trust the consensus\n"
        f"4. If std is high (>0.10), there's genuine uncertainty — widen the estimate\n"
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
        temperature=0.3,  # low temp for final synthesis
        max_tokens=1500,
    )

    if not response:
        # Fallback to simple trimmed mean
        sorted_probs = sorted(r2_probs)
        if len(sorted_probs) >= 4:
            # Trim top and bottom 20%
            trim = max(1, len(sorted_probs) // 5)
            trimmed = sorted_probs[trim:-trim]
            final_p = sum(trimmed) / len(trimmed)
        else:
            final_p = mean_p
        confidence = max(0.3, 1.0 - std_p * 5)
        return final_p, confidence, "Fallback: trimmed mean (LLM synthesis failed)"

    # Extract probability
    final_p = _extract_probability(response)
    if final_p is None:
        final_p = mean_p  # fallback

    # Extract confidence
    conf_match = re.search(r'CONFIDENCE:\s*(0\.\d+)', response, re.IGNORECASE)
    if conf_match:
        confidence = float(conf_match.group(1))
    else:
        confidence = max(0.3, 1.0 - std_p * 5)

    # Extract reasoning (look for numbered points)
    reasoning_lines = []
    for line in response.split("\n"):
        line = line.strip()
        if re.match(r'^[1-3][\.\)]\s', line) or line.startswith("- "):
            reasoning_lines.append(line[:150])
        if len(reasoning_lines) >= 3:
            break
    key_reasoning = " | ".join(reasoning_lines) if reasoning_lines else response[:300]

    return final_p, confidence, key_reasoning


def run_delphi_simulation(market: SportMarket, context: str) -> Optional[CrowdSignal]:
    """
    Run the full 3-round Delphi simulation on a sport market.
    Returns CrowdSignal or None if simulation fails.
    """
    logger.info(
        f"[CROWD-SPORT] Delphi simulation starting: {market.question[:60]}..."
    )

    # ── Round 1: Independent estimates ──
    t0 = time.time()
    r1_results = _run_round1(market, context)
    t1 = time.time()

    if len(r1_results) < 3:
        logger.warning(
            f"[CROWD-SPORT] Round 1 too few estimates ({len(r1_results)}/10), aborting"
        )
        return None

    r1_probs = [p for _, p, _ in r1_results]
    r1_mean = sum(r1_probs) / len(r1_probs)
    logger.info(
        f"[CROWD-SPORT] Round 1 ({t1-t0:.0f}s): {len(r1_results)} groups, "
        f"mean={r1_mean:.3f}, range=[{min(r1_probs):.3f}, {max(r1_probs):.3f}]"
    )

    # ── Round 2: Delphi revision ──
    t2_start = time.time()
    r2_results = _run_round2(market, context, r1_results)
    t2 = time.time()

    if len(r2_results) < 3:
        logger.warning(
            f"[CROWD-SPORT] Round 2 too few revisions ({len(r2_results)}), using R1"
        )
        r2_results = r1_results

    r2_probs = [p for _, p, _ in r2_results]
    r2_mean = sum(r2_probs) / len(r2_probs)
    r2_std = (sum((p - r2_mean)**2 for p in r2_probs) / len(r2_probs)) ** 0.5
    logger.info(
        f"[CROWD-SPORT] Round 2 ({t2-t2_start:.0f}s): {len(r2_results)} groups, "
        f"mean={r2_mean:.3f}, std={r2_std:.3f}"
    )

    # ── Round 3: Final consensus ──
    t3_start = time.time()
    final_p, confidence, key_reasoning = _run_round3(market, context, r1_results, r2_results)
    t3 = time.time()

    logger.info(
        f"[CROWD-SPORT] Round 3 ({t3-t3_start:.0f}s): final={final_p:.3f}, "
        f"confidence={confidence:.2f}"
    )

    # ── Signal generation ──
    yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5

    if final_p > yes_price:
        edge = final_p - yes_price
        side = "BUY_YES"
        # Kelly: f* = (p - price) / (1 - price)
        kelly = (final_p - yes_price) / (1 - yes_price) if yes_price < 1 else 0
    else:
        edge = (1 - final_p) - (1 - yes_price)
        side = "BUY_NO"
        kelly = ((1 - final_p) - (1 - yes_price)) / yes_price if yes_price > 0 else 0

    # Apply Kelly fraction and confidence scaling
    kelly_size = min(MAX_BET, max(5.0, kelly * KELLY_FRACTION * 1000 * confidence))

    # Select token_id based on side
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
        f"[CROWD-SPORT] COMPLETE ({total_time:.0f}s): {market.question[:50]} | "
        f"crowd={final_p:.3f} vs PM={yes_price:.3f} | "
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
    )


# ── Market Fetching ────────────────────────────────────────────────

def fetch_sport_markets() -> list[SportMarket]:
    """Fetch active sport markets from Polymarket Gamma events API."""
    all_markets = []

    try:
        resp = requests.get(
            GAMMA_EVENTS_API,
            params={"closed": "false", "limit": 100},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"[CROWD-SPORT] Events API {resp.status_code}")
            return []

        events = resp.json()
        for event in events:
            title = (event.get("title", "") + " " + event.get("slug", "")).lower()
            if not any(kw in title for kw in SPORT_KEYWORDS):
                continue

            for m in event.get("markets", []):
                try:
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

                    # Token IDs for ordering
                    token_ids = []
                    clob_ids = m.get("clobTokenIds", [])
                    if isinstance(clob_ids, str):
                        try:
                            clob_ids = json.loads(clob_ids)
                        except Exception:
                            clob_ids = []
                    token_ids = clob_ids if clob_ids else []

                    all_markets.append(SportMarket(
                        condition_id=cid,
                        question=m.get("question", "") or m.get("title", ""),
                        event_title=event.get("title", ""),
                        outcomes=outcomes,
                        outcome_prices=prices,
                        volume=vol,
                        end_date=m.get("endDate", ""),
                        neg_risk=m.get("negRisk", False),
                        token_ids=token_ids,
                        description=m.get("description", "")[:500],
                        slug=m.get("slug", ""),
                    ))
                except Exception as e:
                    logger.debug(f"[CROWD-SPORT] Market parse error: {e}")

    except Exception as e:
        logger.error(f"[CROWD-SPORT] Events API error: {e}")

    # Deduplicate by condition_id
    seen = set()
    unique = []
    for m in all_markets:
        if m.condition_id not in seen:
            seen.add(m.condition_id)
            unique.append(m)

    logger.info(f"[CROWD-SPORT] Found {len(unique)} sport markets (vol >= ${MIN_VOLUME:,})")
    return unique


# ── Cache Management ───────────────────────────────────────────────

def _cache_key(market_id: str) -> str:
    return hashlib.md5(market_id.encode()).hexdigest()[:12]


def _get_cached_signal(market_id: str) -> Optional[CrowdSignal]:
    """Check if we have a recent simulation for this market."""
    cache_file = CACHE_DIR / f"{_cache_key(market_id)}.json"
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
    cache_file = CACHE_DIR / f"{_cache_key(signal.market_id)}.json"
    cache_file.write_text(json.dumps(asdict(signal), indent=2))


# ── Strategy Class ─────────────────────────────────────────────────

class CrowdSportStrategy:
    """
    Multi-Agent Crowd Simulation strategy for sport markets.
    Uses Delphi method with 10 specialist groups across 3 rounds.
    """

    # AutoOptimizer parameter space
    MIN_EDGE = MIN_EDGE
    MAX_BET = MAX_BET
    KELLY_FRACTION = KELLY_FRACTION
    MIN_VOLUME = MIN_VOLUME
    MAX_MARKETS_PER_SCAN = MAX_MARKETS_PER_SCAN
    SCAN_INTERVAL_CYCLES = 50  # run every 50 cycles (~25 min)

    def __init__(self, api=None, risk=None):
        self.api = api
        self.risk = risk
        self._total_predictions = 0
        self._total_trades = 0
        self._total_pnl = 0.0
        self._last_scan = 0.0
        self._predictions_history: list[dict] = []
        # Load history
        if RESULTS_FILE.exists():
            try:
                self._predictions_history = json.loads(RESULTS_FILE.read_text())
            except Exception:
                pass

    def scan(self, shared_markets: list = None) -> list[CrowdSignal]:
        """
        Scan top sport markets, run crowd simulation, return opportunities.
        Compatible with bot main loop.

        Args:
            shared_markets: ignored — we fetch sport markets separately from Gamma events API.
        """
        # Check LiteLLM proxy is up
        try:
            resp = requests.get("http://localhost:4000/health", timeout=5)
            if resp.status_code != 200:
                logger.debug("[CROWD-SPORT] LiteLLM proxy not available")
                return []
        except Exception:
            logger.debug("[CROWD-SPORT] LiteLLM proxy not reachable")
            return []

        # Fetch sport markets from Gamma
        markets = fetch_sport_markets()
        if not markets:
            return []

        # v12.8: Filter out extreme longshots and near-certainties
        # Only simulate markets with YES price between 10-80% — that's where mispricing lives
        markets = [m for m in markets
                   if m.outcome_prices and 0.10 <= m.outcome_prices[0] <= 0.80]
        logger.info(f"[CROWD-SPORT] After price filter (10-80%): {len(markets)} markets")

        # Sort by volume descending, take top N
        markets.sort(key=lambda m: m.volume, reverse=True)
        markets = markets[:self.MAX_MARKETS_PER_SCAN]

        signals = []
        for i, market in enumerate(markets):
            # Check cache first
            cached = _get_cached_signal(market.condition_id)
            if cached:
                if cached.edge >= self.MIN_EDGE:
                    # Update price from current market data (may have moved)
                    current_price = market.outcome_prices[0] if market.outcome_prices else 0.5
                    if abs(current_price - cached.polymarket_price) > 0.03:
                        logger.info(
                            f"[CROWD-SPORT] Cache stale (price moved {cached.polymarket_price:.3f} -> "
                            f"{current_price:.3f}), re-simulating"
                        )
                    else:
                        signals.append(cached)
                        logger.info(
                            f"[CROWD-SPORT] [{i+1}/{len(markets)}] CACHED: {market.question[:50]} | "
                            f"edge={cached.edge:.1%}"
                        )
                        continue
                else:
                    logger.debug(
                        f"[CROWD-SPORT] [{i+1}/{len(markets)}] Cached no-edge: {market.question[:40]}"
                    )
                    continue

            # Enrich with real data
            logger.info(
                f"[CROWD-SPORT] [{i+1}/{len(markets)}] Simulating: {market.question[:60]}"
            )
            context = build_enrichment_context(market)

            # Run Delphi simulation
            signal = run_delphi_simulation(market, context)
            if signal:
                self._total_predictions += 1
                _save_cached_signal(signal)

                if signal.edge >= self.MIN_EDGE:
                    signals.append(signal)
                    logger.info(
                        f"[CROWD-SPORT] SIGNAL: {signal.side} edge={signal.edge:.1%} "
                        f"crowd={signal.crowd_probability:.3f} vs PM={signal.polymarket_price:.3f} "
                        f"conf={signal.confidence:.2f} size=${signal.kelly_size:.0f} | "
                        f"{market.question[:50]}"
                    )
                else:
                    logger.info(
                        f"[CROWD-SPORT] No edge: crowd={signal.crowd_probability:.3f} vs "
                        f"PM={signal.polymarket_price:.3f} (edge={signal.edge:.1%} < {self.MIN_EDGE:.0%}) | "
                        f"{market.question[:50]}"
                    )
            else:
                logger.warning(f"[CROWD-SPORT] Simulation failed: {market.question[:50]}")

        # Save predictions history
        if signals:
            for s in signals:
                self._predictions_history.append(asdict(s))
            # Keep last 500
            self._predictions_history = self._predictions_history[-500:]
            RESULTS_FILE.write_text(json.dumps(self._predictions_history, indent=2))

        self._last_scan = time.time()
        logger.info(
            f"[CROWD-SPORT] Scan complete: {len(markets)} markets -> "
            f"{self._total_predictions} simulated -> {len(signals)} signals "
            f"(lifetime: {self._total_predictions} predictions, {self._total_trades} trades)"
        )

        return signals

    def execute(self, signal: CrowdSignal, api=None, risk=None,
                live: bool = False) -> bool:
        """
        Execute a crowd sport trade.

        Args:
            signal: CrowdSignal with edge > MIN_EDGE
            api: PolymarketAPI instance (or use self.api)
            risk: RiskManager instance (or use self.risk)
            live: True for real trading, False for paper
        """
        api = api or self.api
        risk = risk or self.risk

        if not api or not risk:
            logger.error("[CROWD-SPORT] No API or risk manager")
            return False

        # Check risk manager
        size = min(signal.kelly_size, self.MAX_BET)
        can, reason = risk.can_trade(
            strategy="crowd_sport",
            size=size,
            price=signal.polymarket_price,
            side=signal.side,
            market_id=signal.market_id,
        )
        if not can:
            logger.info(f"[CROWD-SPORT] Trade blocked: {reason}")
            return False

        if not live:
            # Paper trade
            logger.info(
                f"[CROWD-SPORT] PAPER {signal.side} ${size:.2f} @ "
                f"{signal.polymarket_price:.3f} | edge={signal.edge:.1%} "
                f"conf={signal.confidence:.2f} | {signal.question[:50]}"
            )
            trade = Trade(
                timestamp=time.time(),
                strategy="crowd_sport",
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side,
                size=size,
                price=signal.polymarket_price,
                edge=signal.edge,
                reason=f"crowd_delphi conf={signal.confidence:.2f} std={signal.std_dev:.3f}",
            )
            risk.open_trade(trade)
            self._total_trades += 1
            return True

        # Live execution
        token_id = signal.token_id
        if not token_id:
            logger.error(f"[CROWD-SPORT] No token_id for {signal.question[:40]}")
            return False

        target_price = signal.polymarket_price
        logger.info(
            f"[CROWD-SPORT] LIVE {signal.side} ${size:.2f} @ "
            f"{target_price:.3f} | edge={signal.edge:.1%} | "
            f"{signal.question[:50]}"
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
                    strategy="crowd_sport",
                    market_id=signal.market_id,
                    token_id=token_id,
                    side=signal.side,
                    size=size,
                    price=target_price,
                    edge=signal.edge,
                    reason=f"crowd_delphi conf={signal.confidence:.2f} std={signal.std_dev:.3f}",
                )
                risk.open_trade(trade)
                self._total_trades += 1
                logger.info(
                    f"[CROWD-SPORT] FILLED: {signal.side} ${size:.2f} | "
                    f"{signal.question[:50]}"
                )
                return True
            else:
                logger.warning(
                    f"[CROWD-SPORT] Order failed: {signal.side} ${size:.2f} | "
                    f"{signal.question[:50]}"
                )
                return False

        except Exception as e:
            logger.error(f"[CROWD-SPORT] Execution error: {e}")
            return False

    @property
    def stats(self) -> dict:
        """Return strategy statistics."""
        accuracy = 0.0
        if self._predictions_history:
            # TODO: track resolution outcomes for accuracy
            pass

        return {
            "total_predictions": self._total_predictions,
            "total_trades": self._total_trades,
            "total_pnl": round(self._total_pnl, 2),
            "predictions_cached": len(list(CACHE_DIR.glob("*.json"))),
            "last_scan": datetime.fromtimestamp(self._last_scan).isoformat() if self._last_scan else "never",
        }


# ── Standalone CLI ─────────────────────────────────────────────────

def main():
    """Run as standalone script for testing."""
    import argparse

    parser = argparse.ArgumentParser(description="Crowd Sport Prediction (Delphi)")
    parser.add_argument("--status", action="store_true", help="Show prediction history")
    parser.add_argument("--scan", action="store_true", help="Run a single scan")
    parser.add_argument("--limit", type=int, default=5, help="Max markets to simulate")
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
            print(f"CROWD SPORT PREDICTIONS — {len(preds)} total")
            print(f"{'='*70}")
            for p in preds[-15:]:
                print(
                    f"  {p['side']} edge={p['edge']:.1%} "
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

    strategy = CrowdSportStrategy()
    strategy.MAX_MARKETS_PER_SCAN = args.limit

    if args.daemon:
        logger.info(f"[CROWD-SPORT] Daemon mode — scan every 4h, limit={args.limit}")
        while True:
            try:
                signals = strategy.scan()
                for s in signals:
                    logger.info(
                        f"  -> {s.side} {s.question[:50]} "
                        f"edge={s.edge:.1%} ${s.kelly_size:.0f}"
                    )
            except Exception as e:
                logger.error(f"[CROWD-SPORT] Scan error: {e}")
            time.sleep(4 * 3600)
    else:
        signals = strategy.scan()
        print(f"\n{len(signals)} signals found:")
        for s in signals:
            print(
                f"  {s.side} edge={s.edge:.1%} crowd={s.crowd_probability:.3f} "
                f"PM={s.polymarket_price:.3f} ${s.kelly_size:.0f} "
                f"conf={s.confidence:.2f} | {s.question[:60]}"
            )
            print(f"    Reasoning: {s.key_reasoning[:120]}")
            print(f"    R1: {s.round1_estimates}")
            print(f"    R2: {s.round2_estimates}")


if __name__ == "__main__":
    main()
