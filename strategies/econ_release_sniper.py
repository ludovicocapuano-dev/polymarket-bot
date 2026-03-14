"""
Economic Data Release Sniper (v12.5)
=====================================
Trades Polymarket markets on US government economic data releases
within ~1 second of publication.

Targets:
- BLS Employment Situation (nonfarm payrolls, unemployment rate) — 1st Friday/month
- BLS CPI (inflation) — ~13th of month
- BEA GDP — quarterly

Strategy:
1. Calendar: parse BLS schedule, set alarms
2. Poll: 100ms polling starting 5s before release time
3. Parse: regex/pd.read_html for headline number
4. Compare: actual vs consensus (FRED + Trading Economics)
5. Trade: if surprise > threshold, buy/sell on Polymarket

Inspired by ArmageddonRewardsBilly ($372K profit, 19 lines of Python).
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Release URLs ──────────────────────────────────────────────

BLS_EMPLOYMENT_URL = "https://www.bls.gov/news.release/empsit.nr0.htm"
BLS_EMPLOYMENT_TABLE = "https://www.bls.gov/news.release/empsit.t17.htm"
BLS_CPI_URL = "https://www.bls.gov/news.release/cpi.nr0.htm"
BLS_SCHEDULE_URL = "https://www.bls.gov/schedule/news_release/empsit.htm"
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred"

# ── Config ────────────────────────────────────────────────────

POLL_INTERVAL_MS = 100  # 100ms between polls
POLL_START_BEFORE_SEC = 10  # start polling 10s before release
POLL_TIMEOUT_SEC = 120  # stop after 2min if no data
REQUEST_TIMEOUT = 0.5  # 500ms HTTP timeout
NFP_SURPRISE_THRESHOLD = 50_000  # ±50K jobs = significant surprise
UNEMPLOYMENT_SURPRISE_THRESHOLD = 0.2  # ±0.2% = significant
CPI_SURPRISE_THRESHOLD = 0.1  # ±0.1% = significant
MAX_BET = 100.0  # max per trade

# ── Data classes ──────────────────────────────────────────────


@dataclass
class EconRelease:
    """Scheduled economic data release."""
    name: str  # "employment_situation", "cpi", "gdp"
    date: datetime  # release datetime (Eastern Time)
    url: str  # URL to poll
    parsed: bool = False
    actual: Optional[float] = None
    consensus: Optional[float] = None


@dataclass
class EconSignal:
    """Trading signal from a data release."""
    release_name: str
    metric: str  # "nonfarm_payrolls", "unemployment_rate", "cpi_yoy"
    actual: float
    consensus: float
    surprise: float  # actual - consensus
    surprise_pct: float  # surprise as % of consensus
    direction: str  # "ABOVE" or "BELOW"
    strength: str  # "STRONG", "MODERATE", "WEAK"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EconReleaseSniper:
    """Snipes economic data releases for Polymarket trading."""

    def __init__(self, api=None, risk=None):
        self.api = api
        self.risk = risk
        self.schedule: list[EconRelease] = []
        self.signals: list[EconSignal] = []
        self._last_schedule_fetch = None
        self._last_nfp: Optional[float] = None
        self._last_unemployment: Optional[float] = None
        self._consensus_cache: dict[str, float] = {}
        self._state_file = Path("logs/econ_sniper_state.json")
        self._load_state()

    def _load_state(self):
        """Load persistent state."""
        if self._state_file.exists():
            try:
                state = json.loads(self._state_file.read_text())
                self._last_nfp = state.get("last_nfp")
                self._last_unemployment = state.get("last_unemployment")
                self._consensus_cache = state.get("consensus_cache", {})
            except Exception:
                pass

    def _save_state(self):
        """Persist state."""
        state = {
            "last_nfp": self._last_nfp,
            "last_unemployment": self._last_unemployment,
            "consensus_cache": self._consensus_cache,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    # ── Schedule Management ───────────────────────────────────

    def fetch_schedule(self) -> list[EconRelease]:
        """Fetch BLS release schedule."""
        now = datetime.now(timezone.utc)
        if self._last_schedule_fetch and (now - self._last_schedule_fetch).total_seconds() < 86400:
            return self.schedule

        # Known 2026 Employment Situation dates (first Friday)
        # Source: https://www.bls.gov/schedule/news_release/empsit.htm
        employment_dates_2026 = [
            "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03",
            "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
            "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
        ]

        # Known 2026 CPI dates (~13th of month)
        cpi_dates_2026 = [
            "2026-01-14", "2026-02-12", "2026-03-11", "2026-04-14",
            "2026-05-13", "2026-06-10", "2026-07-14", "2026-08-12",
            "2026-09-11", "2026-10-13", "2026-11-12", "2026-12-10",
        ]

        # Import/Export Price Indexes (~mid-month, the 17th could be this)
        import_export_dates_2026 = [
            "2026-01-15", "2026-02-13", "2026-03-17", "2026-04-15",
            "2026-05-14", "2026-06-12", "2026-07-16", "2026-08-13",
            "2026-09-15", "2026-10-15", "2026-11-13", "2026-12-11",
        ]

        self.schedule = []
        for date_str in employment_dates_2026:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=13, minute=30, second=0,  # 8:30 AM ET = 13:30 UTC
                tzinfo=timezone.utc,
            )
            if dt > now - timedelta(days=1):  # only future/recent
                self.schedule.append(EconRelease(
                    name="employment_situation",
                    date=dt,
                    url=BLS_EMPLOYMENT_URL,
                ))

        for date_str in cpi_dates_2026:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=13, minute=30, second=0,
                tzinfo=timezone.utc,
            )
            if dt > now - timedelta(days=1):
                self.schedule.append(EconRelease(
                    name="cpi",
                    date=dt,
                    url=BLS_CPI_URL,
                ))

        for date_str in import_export_dates_2026:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=13, minute=30, second=0,
                tzinfo=timezone.utc,
            )
            if dt > now - timedelta(days=1):
                self.schedule.append(EconRelease(
                    name="import_export_prices",
                    date=dt,
                    url="https://www.bls.gov/news.release/ximpim.nr0.htm",
                ))

        self._last_schedule_fetch = now
        logger.info(f"[ECON-SNIPER] Schedule loaded: {len(self.schedule)} upcoming releases")
        return self.schedule

    def next_release(self) -> Optional[EconRelease]:
        """Get the next upcoming release."""
        self.fetch_schedule()
        now = datetime.now(timezone.utc)
        upcoming = [r for r in self.schedule if r.date > now and not r.parsed]
        return min(upcoming, key=lambda r: r.date) if upcoming else None

    def time_to_next_release(self) -> Optional[timedelta]:
        """Time until next release."""
        nxt = self.next_release()
        if nxt:
            return nxt.date - datetime.now(timezone.utc)
        return None

    # ── Consensus Estimates ───────────────────────────────────

    def fetch_consensus(self, metric: str = "nonfarm_payrolls") -> Optional[float]:
        """Fetch consensus estimate from FRED or cache."""
        if metric in self._consensus_cache:
            cached_time = self._consensus_cache.get(f"{metric}_time", "")
            if cached_time:
                try:
                    ct = datetime.fromisoformat(cached_time)
                    if (datetime.now(timezone.utc) - ct).total_seconds() < 86400:
                        return self._consensus_cache[metric]
                except Exception:
                    pass

        # FRED series for previous values (consensus needs external source)
        series_map = {
            "nonfarm_payrolls": "PAYEMS",
            "unemployment_rate": "UNRATE",
            "cpi_yoy": "CPIAUCSL",
        }

        series_id = series_map.get(metric)
        if not series_id or not FRED_API_KEY:
            return self._consensus_cache.get(metric)

        try:
            resp = requests.get(
                f"{FRED_BASE}/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": FRED_API_KEY,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 3,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                obs = resp.json().get("observations", [])
                if obs:
                    val = float(obs[0]["value"])
                    self._consensus_cache[metric] = val
                    self._consensus_cache[f"{metric}_time"] = datetime.now(timezone.utc).isoformat()
                    self._save_state()
                    logger.info(f"[ECON-SNIPER] FRED {metric}: {val}")
                    return val
        except Exception as e:
            logger.debug(f"[ECON-SNIPER] FRED fetch error: {e}")

        return self._consensus_cache.get(metric)

    # ── Data Parsing ──────────────────────────────────────────

    def parse_employment_release(self, html: str) -> dict:
        """Parse BLS Employment Situation release.

        Returns dict with nonfarm_change, unemployment_rate, etc.
        """
        result = {}

        # Nonfarm payroll change — look for pattern like:
        # "Total nonfarm payroll employment rose by 151,000"
        # or "increased by 22,000" or "changed little (+12,000)"
        nfp_patterns = [
            r'(?:rose|increased|added|grew|gained)\s+by\s+([\d,]+)',
            r'(?:fell|decreased|dropped|lost|declined)\s+by\s+([\d,]+)',
            r'changed\s+little\s*\(([+-]?[\d,]+)\)',
            r'payroll\s+employment\s+.*?([\d,]+)\s+in',
        ]

        for i, pattern in enumerate(nfp_patterns):
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                num_str = match.group(1).replace(",", "").replace("+", "")
                try:
                    nfp = int(num_str)
                    # Negative for fell/decreased patterns
                    if i == 1:
                        nfp = -nfp
                    result["nonfarm_change"] = nfp
                    break
                except ValueError:
                    continue

        # Unemployment rate — "unemployment rate was 4.1 percent"
        unemp_match = re.search(
            r'unemployment\s+rate\s+(?:was|at|remained|changed\s+to)\s+([\d.]+)\s*percent',
            html, re.IGNORECASE,
        )
        if unemp_match:
            result["unemployment_rate"] = float(unemp_match.group(1))

        # Average hourly earnings
        earnings_match = re.search(
            r'average\s+hourly\s+earnings.*?(\d+)\s*cent',
            html, re.IGNORECASE,
        )
        if earnings_match:
            result["avg_hourly_earnings_change_cents"] = int(earnings_match.group(1))

        return result

    def parse_cpi_release(self, html: str) -> dict:
        """Parse BLS CPI release."""
        result = {}

        # CPI monthly change — "rose 0.2 percent" or "increased 0.3 percent"
        monthly_match = re.search(
            r'Consumer\s+Price\s+Index.*?(?:rose|increased|fell|decreased)\s+([\d.]+)\s*percent',
            html, re.IGNORECASE,
        )
        if monthly_match:
            result["cpi_monthly"] = float(monthly_match.group(1))

        # YoY — "over the last 12 months... 2.8 percent"
        yoy_match = re.search(
            r'(?:12|twelve)\s+months.*?([\d.]+)\s*percent',
            html, re.IGNORECASE,
        )
        if yoy_match:
            result["cpi_yoy"] = float(yoy_match.group(1))

        return result

    # ── Polling & Sniping ─────────────────────────────────────

    def poll_release(self, release: EconRelease) -> Optional[dict]:
        """Poll a BLS release URL until data appears.

        Returns parsed data dict or None if timeout.
        """
        logger.info(f"[ECON-SNIPER] Polling {release.name} at {release.url}")
        start = time.time()
        last_content_hash = None

        while time.time() - start < POLL_TIMEOUT_SEC:
            try:
                resp = requests.get(release.url, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    time.sleep(POLL_INTERVAL_MS / 1000)
                    continue

                content_hash = hash(resp.text[:500])
                if content_hash == last_content_hash:
                    time.sleep(POLL_INTERVAL_MS / 1000)
                    continue

                last_content_hash = content_hash

                # Try parsing
                if release.name == "employment_situation":
                    data = self.parse_employment_release(resp.text)
                elif release.name == "cpi":
                    data = self.parse_cpi_release(resp.text)
                else:
                    data = {}

                if data:
                    elapsed = time.time() - start
                    logger.info(
                        f"[ECON-SNIPER] PARSED in {elapsed:.1f}s: {data}"
                    )
                    release.parsed = True
                    return data

            except requests.Timeout:
                pass
            except Exception as e:
                logger.debug(f"[ECON-SNIPER] Poll error: {e}")

            time.sleep(POLL_INTERVAL_MS / 1000)

        logger.warning(f"[ECON-SNIPER] Timeout polling {release.name}")
        return None

    # ── Signal Generation ─────────────────────────────────────

    def generate_signal(self, data: dict, release_name: str) -> list[EconSignal]:
        """Generate trading signals from parsed data."""
        signals = []

        if "nonfarm_change" in data:
            actual = data["nonfarm_change"]
            consensus = self.fetch_consensus("nonfarm_payrolls")
            # If we have previous month, estimate consensus as ~similar
            if consensus is None and self._last_nfp is not None:
                consensus = self._last_nfp
            if consensus is not None:
                # For PAYEMS, consensus is total level — we need change
                # Use previous month's change as proxy
                surprise = actual - (consensus if consensus < 1000 else 0)
                direction = "ABOVE" if surprise > 0 else "BELOW"
                strength = "STRONG" if abs(surprise) > 100_000 else "MODERATE" if abs(surprise) > NFP_SURPRISE_THRESHOLD else "WEAK"

                signals.append(EconSignal(
                    release_name=release_name,
                    metric="nonfarm_payrolls",
                    actual=actual,
                    consensus=consensus if consensus < 1000 else 0,
                    surprise=surprise,
                    surprise_pct=abs(surprise) / max(abs(consensus if consensus < 1000 else 150_000), 1) * 100,
                    direction=direction,
                    strength=strength,
                ))
                self._last_nfp = actual
                logger.info(f"[ECON-SNIPER] NFP signal: {actual:+,} vs {consensus:,.0f} expected → {direction} {strength}")

        if "unemployment_rate" in data:
            actual = data["unemployment_rate"]
            consensus = self.fetch_consensus("unemployment_rate") or self._last_unemployment or 4.1
            surprise = actual - consensus
            direction = "ABOVE" if surprise > 0 else "BELOW"
            strength = "STRONG" if abs(surprise) > 0.3 else "MODERATE" if abs(surprise) > UNEMPLOYMENT_SURPRISE_THRESHOLD else "WEAK"

            signals.append(EconSignal(
                release_name=release_name,
                metric="unemployment_rate",
                actual=actual,
                consensus=consensus,
                surprise=surprise,
                surprise_pct=abs(surprise) / consensus * 100,
                direction=direction,
                strength=strength,
            ))
            self._last_unemployment = actual
            logger.info(f"[ECON-SNIPER] Unemployment signal: {actual}% vs {consensus}% → {direction} {strength}")

        if "cpi_yoy" in data:
            actual = data["cpi_yoy"]
            consensus = self.fetch_consensus("cpi_yoy") or 2.8
            surprise = actual - consensus
            direction = "ABOVE" if surprise > 0 else "BELOW"
            strength = "STRONG" if abs(surprise) > 0.2 else "MODERATE" if abs(surprise) > CPI_SURPRISE_THRESHOLD else "WEAK"

            signals.append(EconSignal(
                release_name=release_name,
                metric="cpi_yoy",
                actual=actual,
                consensus=consensus,
                surprise=surprise,
                surprise_pct=abs(surprise) / consensus * 100,
                direction=direction,
                strength=strength,
            ))
            logger.info(f"[ECON-SNIPER] CPI signal: {actual}% vs {consensus}% → {direction} {strength}")

        self._save_state()
        return signals

    # ── Market Discovery ──────────────────────────────────────

    def find_econ_markets(self, keyword: str = "unemployment") -> list[dict]:
        """Find Polymarket markets related to economic data."""
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "closed": "false",
                    "limit": 50,
                    "title": keyword,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                markets = resp.json()
                logger.info(f"[ECON-SNIPER] Found {len(markets)} markets for '{keyword}'")
                return markets
        except Exception as e:
            logger.debug(f"[ECON-SNIPER] Market search error: {e}")
        return []

    def find_all_econ_markets(self) -> dict[str, list[dict]]:
        """Find all economic data markets on Polymarket."""
        keywords = [
            "nonfarm", "payroll", "jobs added", "unemployment rate",
            "inflation", "CPI", "employment", "GDP",
        ]
        all_markets: dict[str, list[dict]] = {}
        for kw in keywords:
            markets = self.find_econ_markets(kw)
            if markets:
                all_markets[kw] = markets
            time.sleep(0.5)  # rate limit
        return all_markets

    # ── Trading Execution ─────────────────────────────────────

    def match_signal_to_market(
        self, signal: EconSignal, markets: list[dict]
    ) -> list[dict]:
        """Match a signal to tradeable Polymarket markets.

        Returns list of {market, token_id, side, size, price, edge} dicts.
        """
        opportunities = []

        for market in markets:
            question = (market.get("question") or market.get("title") or "").lower()
            outcomes = market.get("outcomes", [])
            prices = market.get("outcomePrices", [])

            if not outcomes or not prices:
                continue

            # Match unemployment rate markets
            if signal.metric == "unemployment_rate" and "unemployment" in question:
                # Check if market asks about a specific rate
                for rate_str in re.findall(r'(\d+\.\d+)%?', question):
                    rate = float(rate_str)
                    if abs(rate - signal.actual) < 0.05:
                        # This rate matches actual → YES is likely correct
                        yes_price = float(prices[0]) if prices else 0
                        if yes_price < 0.85:  # still room to profit
                            opportunities.append({
                                "market": market,
                                "side": "BUY_YES",
                                "reason": f"Unemployment actual={signal.actual}% matches market {rate}%",
                                "edge": 1.0 - yes_price - 0.05,  # estimated edge
                                "price": yes_price,
                                "size": min(MAX_BET, self.risk.available_budget("econ_sniper") if self.risk else MAX_BET),
                            })

            # Match nonfarm payroll markets
            if signal.metric == "nonfarm_payrolls" and any(
                w in question for w in ["jobs", "nonfarm", "payroll"]
            ):
                # Check bracket markets (e.g., "0-50K", "50-100K")
                bracket_match = re.search(r'(\d+)[k]?\s*[-–]\s*(\d+)[k]?', question, re.IGNORECASE)
                if bracket_match:
                    lo = int(bracket_match.group(1)) * 1000
                    hi = int(bracket_match.group(2)) * 1000
                    if lo <= signal.actual <= hi:
                        yes_price = float(prices[0]) if prices else 0
                        if yes_price < 0.85:
                            opportunities.append({
                                "market": market,
                                "side": "BUY_YES",
                                "reason": f"NFP actual={signal.actual:,} in bracket [{lo:,}-{hi:,}]",
                                "edge": 1.0 - yes_price - 0.05,
                                "price": yes_price,
                                "size": min(MAX_BET, self.risk.available_budget("econ_sniper") if self.risk else MAX_BET),
                            })

        logger.info(f"[ECON-SNIPER] Matched {len(opportunities)} opportunities for {signal.metric}")
        return opportunities

    def execute_opportunity(self, opp: dict, live: bool = False) -> bool:
        """Execute a trading opportunity."""
        if not self.api or not live:
            logger.info(f"[ECON-SNIPER] PAPER: {opp['side']} ${opp['size']:.0f} @ {opp['price']:.3f} | {opp['reason']}")
            return True

        market = opp["market"]
        token_id = None

        # Get token_id from market
        tokens = market.get("clobTokenIds", [])
        if opp["side"] == "BUY_YES" and tokens:
            token_id = tokens[0]
        elif opp["side"] == "BUY_NO" and len(tokens) > 1:
            token_id = tokens[1]

        if not token_id:
            logger.warning(f"[ECON-SNIPER] No token_id for market")
            return False

        try:
            result = self.api.smart_buy(
                token_id=token_id,
                amount=opp["size"],
                target_price=opp["price"] + 0.05,  # willing to pay up to 5c more
            )
            if result:
                logger.info(f"[ECON-SNIPER] EXECUTED: {opp['side']} ${opp['size']:.0f} | {opp['reason']}")
                if self.risk:
                    self.risk.register_trade(
                        strategy="econ_sniper",
                        market_id=market.get("condition_id", ""),
                        token_id=token_id,
                        side=opp["side"],
                        size=opp["size"],
                        price=opp["price"],
                        edge=opp.get("edge", 0),
                        reason=opp["reason"],
                    )
                return True
            else:
                logger.warning(f"[ECON-SNIPER] Order failed")
                return False
        except Exception as e:
            logger.error(f"[ECON-SNIPER] Execution error: {e}")
            return False

    # ── Main Entry Points ─────────────────────────────────────

    def scan(self, markets: list = None) -> list[dict]:
        """Check if a release is imminent and snipe it.

        Called from bot main loop. Returns opportunities if any.
        """
        nxt = self.next_release()
        if not nxt:
            return []

        ttl = self.time_to_next_release()
        if not ttl:
            return []

        # Only activate within 5 minutes of release
        if ttl.total_seconds() > 300:
            return []

        logger.info(f"[ECON-SNIPER] Next release: {nxt.name} in {ttl.total_seconds():.0f}s")

        # If within polling window, start sniping
        if ttl.total_seconds() <= POLL_START_BEFORE_SEC:
            data = self.poll_release(nxt)
            if data:
                signals = self.generate_signal(data, nxt.name)
                # Find markets to trade
                econ_markets = self.find_all_econ_markets()
                all_opps = []
                for signal in signals:
                    # Get relevant markets for this signal
                    relevant = []
                    for kw_markets in econ_markets.values():
                        relevant.extend(kw_markets)
                    opps = self.match_signal_to_market(signal, relevant)
                    all_opps.extend(opps)
                return all_opps

        return []

    def status(self) -> dict:
        """Return current status."""
        nxt = self.next_release()
        ttl = self.time_to_next_release()
        return {
            "next_release": nxt.name if nxt else None,
            "next_date": nxt.date.isoformat() if nxt else None,
            "time_to_release": str(ttl) if ttl else None,
            "last_nfp": self._last_nfp,
            "last_unemployment": self._last_unemployment,
            "signals_generated": len(self.signals),
            "consensus_cache": {k: v for k, v in self._consensus_cache.items() if not k.endswith("_time")},
        }
