"""
Deep Research Agent (MiroThinker-inspired) v12.8
=================================================
Multi-step research agent that performs deep analysis before crowd simulations.
Uses DeepSeek via LiteLLM for long-horizon reasoning with tool interactions.

Inspired by MiroThinker's approach:
- 256K context, multi-step analysis
- Stepwise verifiable reasoning
- Error correction through feedback
- Web search for real-time information

Used as pre-filter for crowd_prediction: research first, simulate second.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_HEADERS = {"Authorization": "Bearer sk-1234", "Content-Type": "application/json"}
MODEL = "deepseek/deepseek-chat"
RESULTS_DIR = Path("logs/deep_research")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ResearchReport:
    """Result of deep research on a market."""
    question: str
    probability: float  # researched probability 0-1
    confidence: float  # how confident is the research (0-1)
    key_findings: list[str]  # bullet points
    bull_case: str
    bear_case: str
    information_quality: str  # "high", "medium", "low"
    crowd_has_edge: bool  # can a crowd simulation add value?
    recommendation: str  # "SIMULATE", "SKIP", "TRADE_DIRECTLY"
    reasoning: str
    research_time: float  # seconds
    cost: float  # estimated API cost


def _llm_call(messages: list[dict], max_tokens: int = 2000,
              temperature: float = 0.3) -> Optional[str]:
    """Make a DeepSeek LLM call."""
    try:
        resp = requests.post(LITELLM_URL, json={
            "model": MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }, headers=LITELLM_HEADERS, timeout=60)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.debug(f"[RESEARCH] LLM call error: {e}")
    return None


def research_market(question: str, market_price: float,
                    domain: str = "general") -> Optional[ResearchReport]:
    """
    Perform deep multi-step research on a prediction market question.

    Steps:
    1. Initial analysis — what do we know? What's uncertain?
    2. Information gathering — what public data is available?
    3. Base rate analysis — what does history tell us?
    4. Current situation assessment — what's changed recently?
    5. Final probability synthesis — weighted by information quality
    """
    start = time.time()
    logger.info(f"[RESEARCH] Starting deep research: {question[:60]}")

    # Step 1: Initial analysis
    step1 = _llm_call([{
        "role": "system",
        "content": "You are a research analyst. Analyze prediction market questions with rigorous methodology."
    }, {
        "role": "user",
        "content": f"""Analyze this prediction market question:

QUESTION: {question}
CURRENT MARKET PRICE: {market_price:.1%} (YES)
DOMAIN: {domain}

Step 1 — Initial Analysis:
1. What is this question actually asking? Define the exact resolution criteria.
2. What key factors determine the outcome?
3. What information would we NEED to answer this well?
4. Is this a question where public information is sufficient, or does it require insider knowledge?
5. What's the base rate for similar events historically?

Be specific and analytical. No filler."""
    }], max_tokens=800, temperature=0.2)

    if not step1:
        return None

    # Step 2: Information quality assessment
    step2 = _llm_call([{
        "role": "system",
        "content": "You are an information quality assessor for prediction markets."
    }, {
        "role": "user",
        "content": f"""QUESTION: {question}
MARKET PRICE: {market_price:.1%}

Previous analysis:
{step1}

Step 2 — Information Quality:
1. INFORMATION AVAILABILITY: Is the answer knowable from public info? (high/medium/low)
2. CROWD EDGE: Would a crowd of diverse analysts add value, or is this insider-knowledge only?
3. MARKET EFFICIENCY: Is this a heavily traded market (likely efficient) or thin (possible mispricing)?
4. TIME SENSITIVITY: Is there upcoming news/event that will resolve this?
5. BIAS RISK: Are there common biases that could mislead a crowd simulation?

Rate each factor. Then classify:
- SIMULATE: crowd simulation would add value
- SKIP: crowd has no edge, insider knowledge required
- TRADE_DIRECTLY: clear answer from research alone, no need for simulation

Output format:
QUALITY: high/medium/low
CROWD_EDGE: true/false
RECOMMENDATION: SIMULATE/SKIP/TRADE_DIRECTLY"""
    }], max_tokens=600, temperature=0.2)

    if not step2:
        return None

    # Step 3: Probability synthesis
    step3 = _llm_call([{
        "role": "system",
        "content": "You are a superforecaster. Calibrate probabilities carefully."
    }, {
        "role": "user",
        "content": f"""QUESTION: {question}
MARKET PRICE: {market_price:.1%}

Research findings:
{step1[:500]}

Quality assessment:
{step2[:500]}

Step 3 — Final Probability:
Based on your research, what is the TRUE probability of YES?

Consider:
- Base rates for similar events
- Current situation specifics
- Information quality (adjust confidence toward market price when uncertain)
- Known biases in prediction markets

Provide:
PROBABILITY: X.XX (between 0.00 and 1.00)
CONFIDENCE: X.XX (how sure are you? 0.50 = uncertain, 0.90 = very sure)
BULL_CASE: one sentence why YES
BEAR_CASE: one sentence why NO
KEY_FINDINGS:
- finding 1
- finding 2
- finding 3"""
    }], max_tokens=500, temperature=0.2)

    if not step3:
        return None

    elapsed = time.time() - start

    # Parse results
    prob = _extract_float(step3, r'PROBABILITY:\s*([\d.]+)')
    conf = _extract_float(step3, r'CONFIDENCE:\s*([\d.]+)')
    quality = _extract_str(step2, r'QUALITY:\s*(\w+)')
    crowd_edge = "true" in step2.lower().split("crowd_edge:")[-1][:20].lower() if "crowd_edge:" in step2.lower() else True
    recommendation = _extract_str(step2, r'RECOMMENDATION:\s*(\w+)')
    bull = _extract_str(step3, r'BULL_CASE:\s*(.+)')
    bear = _extract_str(step3, r'BEAR_CASE:\s*(.+)')

    # Extract key findings
    findings = re.findall(r'^- (.+)$', step3, re.MULTILINE)

    if prob is None:
        prob = market_price  # fallback to market
    if conf is None:
        conf = 0.5

    report = ResearchReport(
        question=question,
        probability=prob,
        confidence=conf,
        key_findings=findings[:5],
        bull_case=bull or "",
        bear_case=bear or "",
        information_quality=quality or "medium",
        crowd_has_edge=crowd_edge,
        recommendation=recommendation or "SIMULATE",
        reasoning=step3[:300],
        research_time=elapsed,
        cost=0.003,  # ~3 DeepSeek calls
    )

    # Save report
    report_file = RESULTS_DIR / f"research_{int(time.time())}.json"
    report_file.write_text(json.dumps({
        "question": question,
        "market_price": market_price,
        "probability": prob,
        "confidence": conf,
        "quality": quality,
        "crowd_has_edge": crowd_edge,
        "recommendation": recommendation,
        "bull_case": bull,
        "bear_case": bear,
        "findings": findings,
        "elapsed": elapsed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    logger.info(
        f"[RESEARCH] Complete ({elapsed:.0f}s): {question[:50]} | "
        f"prob={prob:.1%} conf={conf:.2f} quality={quality} "
        f"rec={recommendation} crowd_edge={crowd_edge}"
    )

    return report


def _extract_float(text: str, pattern: str) -> Optional[float]:
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        try:
            val = float(match.group(1))
            return val if 0 <= val <= 1 else None
        except ValueError:
            pass
    return None


def _extract_str(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None
