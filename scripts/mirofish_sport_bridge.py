"""
MiroFish Sport Prediction Bridge (v12.5.3)
==========================================
Feeds NBA/NFL/sport data into MiroFish, gets crowd simulation predictions,
compares with Polymarket prices, and generates trading signals.

Flow:
1. Fetch sport markets from Polymarket (Gamma API)
2. Gather seed data (stats, recent form, matchup history) from ESPN/free APIs
3. Send to MiroFish for crowd simulation
4. Compare MiroFish consensus vs Polymarket price
5. If edge > Kelly threshold, signal a trade

Usage:
    python3 scripts/mirofish_sport_bridge.py              # single scan
    python3 scripts/mirofish_sport_bridge.py --daemon      # continuous
    python3 scripts/mirofish_sport_bridge.py --status      # show results
"""

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("mirofish_bridge")

MIROFISH_URL = "http://localhost:5001"
GAMMA_API = "https://gamma-api.polymarket.com/markets"
RESULTS_FILE = Path("logs/mirofish_predictions.json")
SEED_DIR = Path("logs/mirofish_seeds")
SEED_DIR.mkdir(parents=True, exist_ok=True)

SPORT_KEYWORDS = [
    "NBA", "NFL", "MLB", "NHL", "Premier League", "Champions League",
    "La Liga", "Serie A", "Bundesliga", "NCAA", "UFC", "boxing",
    "Super Bowl", "World Series", "Stanley Cup", "Finals",
    "win the", "beat", "playoffs", "championship",
    "Lakers", "Celtics", "Warriors", "Knicks", "Bulls",
    "Yankees", "Dodgers", "Chiefs", "Eagles", "49ers",
    "Manchester", "Liverpool", "Barcelona", "Real Madrid",
    "IPL", "cricket", "tennis", "Wimbledon", "Grand Slam",
]

MIN_VOLUME = 10_000  # $10K minimum volume
MIN_EDGE = 0.05  # 5% minimum edge to trade
MAX_BET = 50.0


@dataclass
class SportPrediction:
    market_id: str
    question: str
    polymarket_price: float
    mirofish_probability: float
    edge: float
    side: str  # BUY_YES or BUY_NO
    confidence: float
    kelly_size: float
    timestamp: str


def fetch_sport_markets() -> list[dict]:
    """Fetch active sport markets from Polymarket via events API."""
    sport_keywords = [
        "nba", "nfl", "nhl", "mlb", "ipl", "premier league",
        "champions league", "la liga", "bundesliga", "serie a",
        "oscars", "academy awards", "stanley cup", "super bowl",
        "world series", "ufc", "tennis", "wimbledon", "grand slam",
    ]

    all_markets = []
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"closed": "false", "limit": 100},
            timeout=15,
        )
        if resp.status_code == 200:
            events = resp.json()
            for event in events:
                title = (event.get("title", "") + event.get("slug", "")).lower()
                if any(kw in title for kw in sport_keywords):
                    for market in event.get("markets", []):
                        market["_event_title"] = event.get("title", "")
                        all_markets.append(market)
    except Exception as e:
        logger.error(f"Events API error: {e}")

    # Filter by volume
    filtered = []
    seen = set()
    for m in all_markets:
        cid = m.get("conditionId") or m.get("condition_id", "")
        vol = float(m.get("volume", 0) or 0)
        if cid and cid not in seen and vol >= MIN_VOLUME:
            seen.add(cid)
            filtered.append(m)

    logger.info(f"[MIROFISH] Found {len(filtered)} sport markets (vol >= ${MIN_VOLUME:,}) from {len(all_markets)} total")
    return filtered


def build_seed_text(market: dict) -> str:
    """Build seed text for MiroFish from market data."""
    question = market.get("question") or market.get("title") or ""
    description = market.get("description") or ""
    outcomes = market.get("outcomes", [])
    prices = market.get("outcomePrices", [])
    volume = market.get("volume", 0)
    end_date = market.get("endDate", "")

    seed = f"""## Prediction Market Analysis Request

**Question:** {question}

**Description:** {description}

**Current Market Data:**
- Outcomes: {', '.join(outcomes) if outcomes else 'Yes/No'}
- Current prices: {', '.join(str(p) for p in prices) if prices else 'N/A'}
- Trading volume: ${float(volume):,.0f}
- Resolution date: {end_date}

**Task:** Simulate a crowd of sports analysts, statisticians, bettors, and insiders.
Each agent should independently assess the probability of the outcome based on:
1. Historical performance and current form
2. Head-to-head matchup history
3. Injuries, suspensions, roster changes
4. Home/away advantage
5. Recent momentum and morale
6. Betting line movements and sharp money indicators

After deliberation, provide a consensus probability (0.0 to 1.0) for the YES outcome.
Format your final answer as: PROBABILITY: X.XX
"""
    return seed


def submit_to_mirofish(seed_text: str, project_name: str) -> Optional[str]:
    """Submit seed data to MiroFish and get prediction."""
    # Save seed as temp file
    seed_file = SEED_DIR / f"seed_{int(time.time())}.txt"
    seed_file.write_text(seed_text)

    try:
        # Step 1: Generate ontology (upload seed + requirement)
        with open(seed_file, "rb") as f:
            resp = requests.post(
                f"{MIROFISH_URL}/api/graph/ontology/generate",
                files={"files": (seed_file.name, f, "text/plain")},
                data={
                    "simulation_requirement": "Predict the probability of the sporting event outcome",
                    "project_name": project_name,
                },
                timeout=120,
            )

        if resp.status_code != 200:
            logger.warning(f"[MIROFISH] Ontology failed: {resp.status_code} {resp.text[:200]}")
            return None

        result = resp.json()
        if not result.get("success"):
            logger.warning(f"[MIROFISH] Ontology error: {result.get('error', '?')}")
            return None

        project_id = result["data"]["project_id"]
        logger.info(f"[MIROFISH] Project created: {project_id}")

        # Step 2: Build graph
        resp = requests.post(
            f"{MIROFISH_URL}/api/graph/build",
            json={"project_id": project_id},
            timeout=180,
        )
        if resp.status_code != 200:
            logger.warning(f"[MIROFISH] Graph build failed: {resp.status_code}")
            return None

        graph_data = resp.json()
        graph_id = graph_data.get("data", {}).get("graph_id")
        if not graph_id:
            # Check if task-based
            task_id = graph_data.get("data", {}).get("task_id")
            if task_id:
                # Poll for completion
                for _ in range(60):
                    time.sleep(3)
                    status_resp = requests.get(
                        f"{MIROFISH_URL}/api/graph/task/{task_id}", timeout=10,
                    )
                    if status_resp.status_code == 200:
                        status_data = status_resp.json().get("data", {})
                        if status_data.get("status") == "completed":
                            graph_id = status_data.get("graph_id")
                            break
                        elif status_data.get("status") == "failed":
                            logger.warning("[MIROFISH] Graph build failed")
                            return None

        if not graph_id:
            logger.warning("[MIROFISH] No graph_id obtained")
            return None

        logger.info(f"[MIROFISH] Graph built: {graph_id}")

        # Step 3: Create simulation
        resp = requests.post(
            f"{MIROFISH_URL}/api/simulation/create",
            json={
                "project_id": project_id,
                "graph_id": graph_id,
                "enable_twitter": True,
                "enable_reddit": True,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(f"[MIROFISH] Simulation create failed: {resp.status_code}")
            return None

        sim_data = resp.json()
        simulation_id = sim_data.get("data", {}).get("simulation_id")
        logger.info(f"[MIROFISH] Simulation created: {simulation_id}")

        # Step 4: Prepare simulation (agent profiles + config)
        resp = requests.post(
            f"{MIROFISH_URL}/api/simulation/prepare",
            json={"simulation_id": simulation_id},
            timeout=300,
        )

        # Step 5: Generate report
        resp = requests.post(
            f"{MIROFISH_URL}/api/report/generate",
            json={
                "simulation_id": simulation_id,
                "query": "What is the probability of the YES outcome? Provide a number between 0.0 and 1.0.",
            },
            timeout=300,
        )

        if resp.status_code == 200:
            report = resp.json()
            report_text = report.get("data", {}).get("content", "")
            return report_text
        else:
            logger.warning(f"[MIROFISH] Report failed: {resp.status_code}")
            return None

    except Exception as e:
        logger.error(f"[MIROFISH] Error: {e}")
        return None
    finally:
        seed_file.unlink(missing_ok=True)


def extract_probability(report_text: str) -> Optional[float]:
    """Extract probability from MiroFish report."""
    if not report_text:
        return None

    # Look for explicit PROBABILITY: X.XX
    match = re.search(r'PROBABILITY:\s*([\d.]+)', report_text, re.IGNORECASE)
    if match:
        try:
            p = float(match.group(1))
            if 0 <= p <= 1:
                return p
        except ValueError:
            pass

    # Look for percentage patterns: "63%", "0.63", "63 percent"
    patterns = [
        r'(\d{1,2}(?:\.\d+)?)\s*%',  # 63% or 63.5%
        r'probability\s+(?:of\s+)?(?:is\s+)?(?:about\s+)?(?:approximately\s+)?(0\.\d+)',  # probability is 0.63
        r'consensus\s+(?:is\s+)?(0\.\d+)',  # consensus is 0.63
        r'estimate[sd]?\s+(?:at\s+)?(0\.\d+)',  # estimated at 0.63
    ]
    for pattern in patterns:
        match = re.search(pattern, report_text, re.IGNORECASE)
        if match:
            try:
                val = float(match.group(1))
                if val > 1:  # percentage
                    val /= 100
                if 0 <= val <= 1:
                    return val
            except ValueError:
                continue

    return None


def generate_signal(market: dict, mirofish_prob: float) -> Optional[SportPrediction]:
    """Compare MiroFish probability with Polymarket price."""
    prices = market.get("outcomePrices", [])
    if not prices:
        return None

    yes_price = float(prices[0]) if prices else 0.5
    question = market.get("question") or market.get("title") or ""
    market_id = market.get("conditionId") or market.get("condition_id", "")

    # Edge calculation
    if mirofish_prob > yes_price:
        # MiroFish thinks YES is more likely than market → BUY YES
        edge = mirofish_prob - yes_price
        side = "BUY_YES"
    else:
        # MiroFish thinks NO is more likely → BUY NO
        edge = (1 - mirofish_prob) - (1 - yes_price)
        side = "BUY_NO"

    if edge < MIN_EDGE:
        logger.info(f"[MIROFISH] Skip: edge {edge:.3f} < {MIN_EDGE} | {question[:50]}")
        return None

    # Kelly sizing
    if side == "BUY_YES":
        kelly = (mirofish_prob - yes_price) / (1 - yes_price)
    else:
        kelly = ((1 - mirofish_prob) - (1 - yes_price)) / yes_price

    kelly_size = min(MAX_BET, max(5, kelly * 0.25 * 1000))  # quarter-Kelly on $1K bankroll

    return SportPrediction(
        market_id=market_id,
        question=question[:100],
        polymarket_price=yes_price,
        mirofish_probability=mirofish_prob,
        edge=round(edge, 4),
        side=side,
        confidence=round(min(1, edge * 10), 2),  # rough confidence
        kelly_size=round(kelly_size, 2),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def run_scan():
    """Full scan: find markets → predict → signal."""
    logger.info("[MIROFISH] Starting sport prediction scan")

    # Check MiroFish is up
    try:
        resp = requests.get(f"{MIROFISH_URL}/health", timeout=5)
        if resp.status_code != 200:
            logger.error("[MIROFISH] Backend not available")
            return []
    except Exception:
        logger.error("[MIROFISH] Backend not reachable")
        return []

    markets = fetch_sport_markets()
    if not markets:
        logger.info("[MIROFISH] No sport markets found")
        return []

    # Limit to top 5 by volume (each simulation costs API calls)
    markets.sort(key=lambda m: float(m.get("volume", 0) or 0), reverse=True)
    markets = markets[:5]

    predictions = []
    for i, market in enumerate(markets):
        question = market.get("question") or market.get("title") or ""
        logger.info(f"[MIROFISH] [{i+1}/{len(markets)}] Predicting: {question[:60]}")

        seed = build_seed_text(market)
        report = submit_to_mirofish(seed, f"sport_{int(time.time())}")

        if report:
            prob = extract_probability(report)
            if prob is not None:
                signal = generate_signal(market, prob)
                if signal:
                    predictions.append(signal)
                    logger.info(
                        f"[MIROFISH] SIGNAL: {signal.side} edge={signal.edge:.1%} "
                        f"MF={signal.mirofish_probability:.2f} vs PM={signal.polymarket_price:.2f} "
                        f"size=${signal.kelly_size:.0f} | {question[:50]}"
                    )
                else:
                    logger.info(f"[MIROFISH] No edge: MF={prob:.2f} vs PM price | {question[:50]}")
            else:
                logger.warning(f"[MIROFISH] Could not extract probability from report")
        else:
            logger.warning(f"[MIROFISH] No report generated for: {question[:50]}")

        time.sleep(2)  # rate limit between simulations

    # Save results
    if predictions:
        history = []
        if RESULTS_FILE.exists():
            try:
                history = json.loads(RESULTS_FILE.read_text())
            except Exception:
                pass
        history.extend([asdict(p) for p in predictions])
        history = history[-200:]  # keep last 200
        RESULTS_FILE.write_text(json.dumps(history, indent=2))
        logger.info(f"[MIROFISH] Saved {len(predictions)} predictions to {RESULTS_FILE}")

    return predictions


def show_status():
    """Show latest predictions."""
    if not RESULTS_FILE.exists():
        print("No predictions yet.")
        return

    preds = json.loads(RESULTS_FILE.read_text())
    print(f"\n{'='*70}")
    print(f"MIROFISH SPORT PREDICTIONS — {len(preds)} total")
    print(f"{'='*70}")
    for p in preds[-10:]:
        print(f"  {p['side']} edge={p['edge']:.1%} MF={p['mirofish_probability']:.2f} "
              f"PM={p['polymarket_price']:.2f} ${p['kelly_size']:.0f} | {p['question'][:50]}")


def main():
    parser = argparse.ArgumentParser(description="MiroFish Sport Prediction Bridge")
    parser.add_argument("--daemon", action="store_true", help="Run continuously (every 4h)")
    parser.add_argument("--status", action="store_true", help="Show latest predictions")
    parser.add_argument("--interval", type=float, default=4, help="Hours between scans (daemon)")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.daemon:
        logger.info(f"[MIROFISH] Daemon mode — scan every {args.interval}h")
        while True:
            try:
                run_scan()
            except Exception as e:
                logger.error(f"[MIROFISH] Scan error: {e}")
            time.sleep(args.interval * 3600)
    else:
        run_scan()


if __name__ == "__main__":
    main()
