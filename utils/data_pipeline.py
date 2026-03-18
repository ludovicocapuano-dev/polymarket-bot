"""
Polymarket Data Pipeline (v12.8)
=================================
Four-layer data pipeline:
1. REST API — fetch markets, prices, trades via rate-limited client
2. WebSocket — real-time price updates (existing polymarket_ws_feed.py)
3. Storage — persist to SQLite (market_db.py)
4. Processing — normalize, enrich, query

Runs as background task in the bot, populating the database for analytics.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from utils.market_db import db

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class RateLimiter:
    calls_per_second: float
    _last_call: float = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self._last_call
        min_interval = 1.0 / self.calls_per_second
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call = time.time()


class DataPipeline:
    """Fetches, stores, and processes Polymarket data."""

    def __init__(self, rate_limit: float = 3.0):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "polymarket-bot/12.8",
            "Accept": "application/json",
        })
        self.limiter = RateLimiter(calls_per_second=rate_limit)
        self._request_count = 0

    def _get(self, base: str, endpoint: str, params: dict = None,
             retries: int = 3) -> Optional[dict]:
        url = f"{base}{endpoint}"
        self.limiter.wait()

        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=10)
                resp.raise_for_status()
                self._request_count += 1
                return resp.json()
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    wait = 2 ** attempt
                    logger.debug(f"[PIPELINE] Rate limited, waiting {wait}s")
                    time.sleep(wait)
                elif e.response.status_code >= 500:
                    time.sleep(1)
                else:
                    return None
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                time.sleep(2 ** attempt)

        return None

    # ── Layer 1: REST API ─────────────────────────────────────

    def sync_markets(self, limit: int = 200) -> int:
        """Fetch markets from Gamma API and store in DB."""
        data = self._get(GAMMA_BASE, "/markets", {"limit": limit, "active": "true"})
        if not data:
            return 0

        count = 0
        for m in data:
            try:
                market_id = m.get("id") or m.get("conditionId", "")
                if not market_id:
                    continue

                db.upsert_market(
                    market_id=market_id,
                    condition_id=m.get("conditionId", ""),
                    question=m.get("question", ""),
                    category=m.get("category", ""),
                    volume=float(m.get("volume", 0) or 0),
                    liquidity=float(m.get("liquidity", 0) or 0),
                    active=m.get("active", True),
                    closed=m.get("closed", False),
                    resolved=m.get("resolved", False),
                    end_date=m.get("endDate", ""),
                    neg_risk=m.get("negRisk", False),
                )

                # Store tokens
                import json
                tokens = m.get("clobTokenIds", [])
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except Exception:
                        tokens = []
                outcomes = m.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = ["Yes", "No"]

                for i, tid in enumerate(tokens):
                    outcome = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
                    db.upsert_token(tid, market_id, outcome)

                count += 1
            except Exception:
                continue

        logger.info(f"[PIPELINE] Synced {count} markets to DB")
        return count

    def snapshot_prices(self, market_ids: list[str] = None, limit: int = 50) -> int:
        """Take price snapshots for active markets."""
        if not market_ids:
            active = db.get_active_markets(min_liquidity=5000)
            # Deduplicate by market_id
            seen = set()
            market_ids = []
            for m in active:
                mid = m["market_id"]
                if mid not in seen:
                    seen.add(mid)
                    market_ids.append(mid)
            market_ids = market_ids[:limit]

        count = 0
        for mid in market_ids:
            prices = db.get_latest_prices(mid)
            # Get fresh prices from CLOB
            tokens = []
            with db.connection() as conn:
                rows = conn.execute(
                    "SELECT token_id, outcome FROM outcome_tokens WHERE market_id = ?",
                    (mid,)
                ).fetchall()
                tokens = [(r["token_id"], r["outcome"]) for r in rows]

            for tid, outcome in tokens:
                try:
                    price_data = self._get(CLOB_BASE, "/price",
                                           {"token_id": tid, "side": "BUY"})
                    if price_data and "price" in price_data:
                        price = float(price_data["price"])
                        # Get spread
                        spread_data = self._get(CLOB_BASE, "/spread",
                                                {"token_id": tid})
                        bid = float(spread_data.get("bid", 0)) if spread_data else None
                        ask = float(spread_data.get("ask", 0)) if spread_data else None

                        db.record_price(tid, mid, outcome, price, bid, ask)
                        count += 1
                except Exception:
                    continue

        logger.info(f"[PIPELINE] Recorded {count} price snapshots")
        return count

    def sync_all(self) -> dict:
        """Full sync: markets + prices."""
        markets = self.sync_markets()
        prices = self.snapshot_prices()
        stats = db.get_db_stats()
        logger.info(f"[PIPELINE] Sync complete: {stats}")
        return {"markets_synced": markets, "prices_recorded": prices, **stats}


# Global instance
pipeline = DataPipeline()
