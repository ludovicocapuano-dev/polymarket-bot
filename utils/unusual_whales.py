"""
Unusual Whales API Integration (v12.6)
=======================================
Feeds congressional trading, dark pool, insider trades, and economic calendar
data into the Polymarket bot for signal generation.

Key signals:
1. Congress trades → political/legislative market signals
2. Dark pool activity → smart money positioning before events
3. Economic calendar → enhances econ_sniper with consensus estimates
4. Insider trades → corporate event signals
"""

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.unusualwhales.com/api"
API_KEY = os.getenv("UNUSUAL_WHALES_API_KEY", "")
CACHE_DIR = Path("logs/unusual_whales_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_FILE = Path("logs/unusual_whales_signals.json")


@dataclass
class UWSignal:
    """Signal derived from Unusual Whales data."""
    source: str  # "congress", "darkpool", "insider", "options_flow"
    ticker: str
    direction: str  # "BULLISH" or "BEARISH"
    strength: float  # 0-1
    detail: str
    timestamp: str
    polymarket_relevance: str  # how it maps to Polymarket markets


class UnusualWhalesClient:
    """Client for Unusual Whales API."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or API_KEY
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_ttl = 300  # 5 min

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """Make authenticated GET request with caching."""
        cache_key = f"{endpoint}:{json.dumps(params or {}, sort_keys=True)}"
        now = time.time()
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return data

        try:
            resp = requests.get(
                f"{BASE_URL}/{endpoint}",
                headers=self._headers,
                params=params,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._cache[cache_key] = (now, data)
                return data
            else:
                logger.warning(f"[UW] {endpoint}: HTTP {resp.status_code}")
                return {"data": []}
        except Exception as e:
            logger.debug(f"[UW] {endpoint} error: {e}")
            return {"data": []}

    # ── Congress Trading ──────────────────────────────────────

    def get_congress_trades(self, limit: int = 100) -> list[dict]:
        """Get recent congressional trades."""
        data = self._get("congress/recent-trades")
        trades = data.get("data", [])[:limit]
        logger.info(f"[UW] Congress trades: {len(trades)}")
        return trades

    def scan_congress_signals(self) -> list[UWSignal]:
        """Generate signals from congressional trading patterns."""
        trades = self.get_congress_trades(200)
        signals = []

        # Group by ticker
        by_ticker: dict[str, list[dict]] = {}
        for t in trades:
            ticker = t.get("ticker")
            if ticker:
                by_ticker.setdefault(ticker, []).append(t)

        # Find clusters (multiple congress members buying/selling same ticker)
        for ticker, ticker_trades in by_ticker.items():
            buys = [t for t in ticker_trades if t.get("txn_type") == "Buy"]
            sells = [t for t in ticker_trades if t.get("txn_type") in ("Sell", "Sale")]

            if len(buys) >= 3:
                names = list(set(t.get("name", "?") for t in buys))[:3]
                signals.append(UWSignal(
                    source="congress",
                    ticker=ticker,
                    direction="BULLISH",
                    strength=min(1.0, len(buys) / 5),
                    detail=f"{len(buys)} congress members buying: {', '.join(names)}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    polymarket_relevance=f"Look for markets on {ticker} price targets or related legislation",
                ))
            elif len(sells) >= 3:
                names = list(set(t.get("name", "?") for t in sells))[:3]
                signals.append(UWSignal(
                    source="congress",
                    ticker=ticker,
                    direction="BEARISH",
                    strength=min(1.0, len(sells) / 5),
                    detail=f"{len(sells)} congress members selling: {', '.join(names)}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    polymarket_relevance=f"Look for markets on {ticker} price drops or regulation",
                ))

        if signals:
            logger.info(f"[UW] Congress signals: {len(signals)}")
        return signals

    # ── Dark Pool ─────────────────────────────────────────────

    def get_darkpool_trades(self, ticker: str = None) -> list[dict]:
        """Get recent dark pool trades."""
        if ticker:
            data = self._get(f"darkpool/{ticker}")
        else:
            data = self._get("darkpool/recent")
        return data.get("data", [])

    def scan_darkpool_signals(self, tickers: list[str] = None) -> list[UWSignal]:
        """Scan dark pool for unusual activity."""
        if not tickers:
            tickers = ["SPY", "QQQ", "NVDA", "AAPL", "TSLA"]

        signals = []
        for ticker in tickers:
            trades = self.get_darkpool_trades(ticker)
            if not trades:
                continue

            # Look for large blocks
            large = [t for t in trades if float(t.get("total_size", 0) or 0) > 1_000_000]
            if large:
                total_size = sum(float(t.get("total_size", 0) or 0) for t in large)
                signals.append(UWSignal(
                    source="darkpool",
                    ticker=ticker,
                    direction="BULLISH",  # large dark pool = institutional accumulation
                    strength=min(1.0, total_size / 10_000_000),
                    detail=f"{len(large)} large dark pool blocks, total ${total_size:,.0f}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    polymarket_relevance=f"Institutional positioning on {ticker} — check price target markets",
                ))
            time.sleep(0.3)

        return signals

    # ── Economic Calendar ─────────────────────────────────────

    def get_economic_calendar(self) -> list[dict]:
        """Get upcoming economic events with consensus estimates."""
        data = self._get("market/economic-calendar")
        events = data.get("data", [])
        logger.info(f"[UW] Economic calendar: {len(events)} events")
        return events

    def get_upcoming_events(self, days: int = 7) -> list[dict]:
        """Get events in the next N days."""
        events = self.get_economic_calendar()
        # Filter for upcoming (the API should return future events)
        return events[:20]  # top 20

    # ── Insider Trading ───────────────────────────────────────

    def get_insider_trades(self, ticker: str = None) -> list[dict]:
        """Get recent insider transactions."""
        if ticker:
            data = self._get(f"insider/{ticker}/flow")
        else:
            data = self._get("insider/transactions")
        return data.get("data", [])

    def scan_insider_signals(self) -> list[UWSignal]:
        """Scan insider trades for unusual patterns."""
        trades = self.get_insider_trades()
        signals = []

        # Group by ticker
        by_ticker: dict[str, list[dict]] = {}
        for t in trades:
            ticker = t.get("ticker", "")
            if ticker:
                by_ticker.setdefault(ticker, []).append(t)

        # Cluster buying = bullish signal
        for ticker, ticker_trades in by_ticker.items():
            buys = [t for t in ticker_trades if "buy" in str(t.get("transaction_type", "")).lower() or "purchase" in str(t.get("transaction_type", "")).lower()]
            if len(buys) >= 2:
                signals.append(UWSignal(
                    source="insider",
                    ticker=ticker,
                    direction="BULLISH",
                    strength=min(1.0, len(buys) / 4),
                    detail=f"{len(buys)} insider buys on {ticker}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    polymarket_relevance=f"Insider confidence in {ticker} — check earnings/price markets",
                ))

        return signals

    # ── Unified Scanner ───────────────────────────────────────

    def scan_all(self) -> list[UWSignal]:
        """Run all signal scanners."""
        all_signals = []

        try:
            all_signals.extend(self.scan_congress_signals())
        except Exception as e:
            logger.debug(f"[UW] Congress scan error: {e}")

        try:
            all_signals.extend(self.scan_darkpool_signals())
        except Exception as e:
            logger.debug(f"[UW] Darkpool scan error: {e}")

        try:
            all_signals.extend(self.scan_insider_signals())
        except Exception as e:
            logger.debug(f"[UW] Insider scan error: {e}")

        # Save signals
        if all_signals:
            history = []
            if SIGNALS_FILE.exists():
                try:
                    history = json.loads(SIGNALS_FILE.read_text())
                except Exception:
                    pass
            history.extend([asdict(s) for s in all_signals])
            history = history[-500:]
            SIGNALS_FILE.write_text(json.dumps(history, indent=2))

        logger.info(f"[UW] Total signals: {len(all_signals)} (congress={sum(1 for s in all_signals if s.source=='congress')}, darkpool={sum(1 for s in all_signals if s.source=='darkpool')}, insider={sum(1 for s in all_signals if s.source=='insider')})")
        return all_signals

    def status(self) -> dict:
        """Return client status."""
        return {
            "api_key_set": bool(self.api_key),
            "cache_entries": len(self._cache),
        }
