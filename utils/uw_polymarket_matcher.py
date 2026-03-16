"""
UW-Polymarket Matcher (v12.7)
==============================
Connects Unusual Whales signals to tradeable Polymarket markets.

Signal types matched:
1. Congress trades → company/ticker price target markets, legislation markets
2. Crypto whale tx → crypto price target markets (BTC/ETH/SOL above/below $X)
3. Economic calendar → Fed, jobs, inflation, GDP markets
4. Insider trades → earnings, price target markets on the ticker
5. Dark pool → institutional positioning, price target markets

Conservative approach: only flags high-confidence matches with edge > 5%.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from utils.unusual_whales import UWSignal

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CACHE_FILE = Path("logs/uw_matcher_cache.json")
MATCHES_FILE = Path("logs/uw_matches.json")
CACHE_TTL = 1800  # 30 min

# ── Ticker → company name mapping for fuzzy matching ──
TICKER_TO_COMPANY = {
    "AAPL": "Apple",
    "AMZN": "Amazon",
    "GOOG": "Google",
    "GOOGL": "Google",
    "META": "Meta",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    "AMD": "AMD",
    "NFLX": "Netflix",
    "DIS": "Disney",
    "BA": "Boeing",
    "JPM": "JPMorgan",
    "GS": "Goldman Sachs",
    "MS": "Morgan Stanley",
    "WMT": "Walmart",
    "TGT": "Target",
    "COST": "Costco",
    "PFE": "Pfizer",
    "JNJ": "Johnson",
    "UNH": "UnitedHealth",
    "XOM": "Exxon",
    "CVX": "Chevron",
    "COP": "ConocoPhillips",
    "LMT": "Lockheed",
    "RTX": "Raytheon",
    "GD": "General Dynamics",
    "NOC": "Northrop",
    "INTC": "Intel",
    "TSM": "TSMC",
    "QCOM": "Qualcomm",
    "AVGO": "Broadcom",
    "CRM": "Salesforce",
    "ORCL": "Oracle",
    "IBM": "IBM",
    "V": "Visa",
    "MA": "Mastercard",
    "PYPL": "PayPal",
    "SQ": "Block",
    "COIN": "Coinbase",
    "HOOD": "Robinhood",
    "AJG": "Arthur J. Gallagher",
    "JLL": "Jones Lang LaSalle",
    "PAYX": "Paychex",
    "GM": "General Motors",
    "F": "Ford",
    "RIVN": "Rivian",
    "LCID": "Lucid",
    "NKE": "Nike",
    "SBUX": "Starbucks",
    "MCD": "McDonald",
    "KO": "Coca-Cola",
    "PEP": "Pepsi",
    "BRK": "Berkshire",
    "SPY": "S&P 500",
    "QQQ": "Nasdaq",
    "IWM": "Russell",
    "DIA": "Dow Jones",
    "TLT": "Treasury",
    "GLD": "Gold",
    "SLV": "Silver",
    "USO": "Oil",
    "UNG": "Natural Gas",
}

# Crypto ticker → token name
CRYPTO_TICKERS = {
    "BTC": ["Bitcoin", "BTC"],
    "ETH": ["Ethereum", "ETH"],
    "SOL": ["Solana", "SOL"],
    "XRP": ["XRP", "Ripple"],
    "DOGE": ["Dogecoin", "DOGE"],
    "ADA": ["Cardano", "ADA"],
    "AVAX": ["Avalanche", "AVAX"],
    "DOT": ["Polkadot", "DOT"],
    "LINK": ["Chainlink", "LINK"],
    "MATIC": ["Polygon", "MATIC"],
    "UNI": ["Uniswap", "UNI"],
    "AAVE": ["Aave", "AAVE"],
}

# Economic event keywords → Polymarket search terms
ECON_KEYWORDS = {
    "FOMC": ["Fed", "interest rate", "rate cut", "rate hike", "Federal Reserve", "FOMC"],
    "CPI": ["inflation", "CPI", "consumer price"],
    "NFP": ["jobs", "employment", "nonfarm", "payroll", "unemployment"],
    "GDP": ["GDP", "economic growth", "recession"],
    "PPI": ["producer price", "PPI", "wholesale"],
    "PCE": ["PCE", "personal consumption", "inflation"],
    "ISM": ["ISM", "manufacturing", "PMI"],
    "RETAIL": ["retail sales", "consumer spending"],
    "HOUSING": ["housing", "home sales", "mortgage"],
    "JOBLESS": ["jobless claims", "unemployment", "initial claims"],
}


@dataclass
class MatchedOpportunity:
    """A matched UW signal → Polymarket market opportunity."""
    signal_source: str
    signal_ticker: str
    signal_direction: str
    signal_strength: float
    signal_detail: str
    market_id: str
    market_question: str
    market_slug: str
    market_yes_price: float
    market_volume: float
    suggested_side: str  # "BUY_YES" or "BUY_NO"
    edge_estimate: float  # estimated edge 0-1
    confidence: float  # match confidence 0-1
    match_reason: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


class UWPolymarketMatcher:
    """Matches Unusual Whales signals to Polymarket markets."""

    def __init__(self):
        self._market_cache: list[dict] = []
        self._cache_ts: float = 0
        self._match_history: list[dict] = []
        # Load match history
        if MATCHES_FILE.exists():
            try:
                self._match_history = json.loads(MATCHES_FILE.read_text())[-200:]
            except Exception:
                pass

    # ── Market Fetching ──────────────────────────────────────

    def _fetch_markets(self, search: str = "", limit: int = 50) -> list[dict]:
        """Fetch markets from Gamma API with optional search."""
        try:
            params = {
                "closed": "false",
                "limit": limit,
                "order": "volume",
                "ascending": "false",
            }
            if search:
                params["tag"] = search
            resp = requests.get(GAMMA_API, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json() if isinstance(resp.json(), list) else []
            logger.debug(f"[UW-MATCH] Gamma API: HTTP {resp.status_code}")
            return []
        except Exception as e:
            logger.debug(f"[UW-MATCH] Gamma API error: {e}")
            return []

    def _search_markets(self, query: str, limit: int = 30) -> list[dict]:
        """Search markets by keyword in question/description."""
        try:
            params = {
                "closed": "false",
                "limit": limit,
            }
            # Gamma API supports text search via the 'slug_like' or direct search
            # We fetch a broad set and filter locally for reliability
            resp = requests.get(GAMMA_API, params=params, timeout=15)
            if resp.status_code != 200:
                return []
            markets = resp.json() if isinstance(resp.json(), list) else []
            # Local filter by keyword
            query_lower = query.lower()
            query_words = query_lower.split()
            matched = []
            for m in markets:
                q = (m.get("question", "") or "").lower()
                desc = (m.get("description", "") or "").lower()
                text = f"{q} {desc}"
                # All query words must appear
                if all(w in text for w in query_words):
                    matched.append(m)
            return matched
        except Exception as e:
            logger.debug(f"[UW-MATCH] Search error: {e}")
            return []

    def _get_cached_markets(self) -> list[dict]:
        """Get cached market list, refresh every 30 min."""
        now = time.time()
        if now - self._cache_ts < CACHE_TTL and self._market_cache:
            return self._market_cache

        try:
            all_markets = []
            for offset in range(0, 300, 100):
                params = {
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                    "order": "volume",
                    "ascending": "false",
                }
                resp = requests.get(GAMMA_API, params=params, timeout=15)
                if resp.status_code == 200:
                    batch = resp.json() if isinstance(resp.json(), list) else []
                    all_markets.extend(batch)
                    if len(batch) < 100:
                        break
                else:
                    break
                time.sleep(0.3)

            if all_markets:
                self._market_cache = all_markets
                self._cache_ts = now
                logger.info(f"[UW-MATCH] Cached {len(all_markets)} Polymarket markets")

                # Save cache to disk
                try:
                    CACHE_FILE.write_text(json.dumps({
                        "ts": now,
                        "count": len(all_markets),
                        "markets": [
                            {
                                "id": m.get("id", ""),
                                "question": m.get("question", ""),
                                "slug": m.get("slug", ""),
                                "outcomePrices": m.get("outcomePrices", ""),
                                "volume": m.get("volume", 0),
                                "tags": m.get("tags", []),
                            }
                            for m in all_markets
                        ],
                    }, indent=2))
                except Exception:
                    pass
            elif not self._market_cache and CACHE_FILE.exists():
                # Load from disk cache
                try:
                    cached = json.loads(CACHE_FILE.read_text())
                    self._market_cache = cached.get("markets", [])
                    logger.info(f"[UW-MATCH] Loaded {len(self._market_cache)} markets from disk cache")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[UW-MATCH] Cache refresh error: {e}")

        return self._market_cache

    # ── Matching Logic ───────────────────────────────────────

    def _text_match_score(self, query_terms: list[str], market: dict) -> float:
        """Score how well query terms match a market. Returns 0-1."""
        q = (market.get("question", "") or "").lower()
        desc = (market.get("description", "") or "").lower()
        slug = (market.get("slug", "") or "").lower()
        text = f"{q} {desc} {slug}"

        if not query_terms:
            return 0.0

        matched = sum(1 for t in query_terms if t.lower() in text)
        return matched / len(query_terms)

    def _parse_price_target(self, question: str) -> Optional[tuple[str, str, float]]:
        """Extract (ticker/asset, direction, target) from market question.

        Examples:
            "Will NVDA be above $150 on March 31?" → ("NVDA", "above", 150.0)
            "Bitcoin above $100,000?" → ("BTC", "above", 100000.0)
            "Will ETH drop below $2,000?" → ("ETH", "below", 2000.0)
        """
        q = question.lower()

        # Pattern: "X above/below $Y"
        pattern = r'([\w\s]+?)\s+(above|below|over|under|exceed|reach|hit)\s+\$?([\d,]+\.?\d*)'
        m = re.search(pattern, q)
        if m:
            asset = m.group(1).strip()
            direction = "above" if m.group(2) in ("above", "over", "exceed", "reach", "hit") else "below"
            target = float(m.group(3).replace(",", ""))
            return (asset, direction, target)

        return None

    def _get_market_yes_price(self, market: dict) -> float:
        """Extract YES price from market data."""
        try:
            prices_str = market.get("outcomePrices", "")
            if isinstance(prices_str, str) and prices_str:
                prices = json.loads(prices_str)
                if isinstance(prices, list) and len(prices) >= 1:
                    return float(prices[0])
            elif isinstance(prices_str, list) and len(prices_str) >= 1:
                return float(prices_str[0])
        except Exception:
            pass
        return 0.5  # default

    def match_congress_signal(self, signal: UWSignal) -> list[MatchedOpportunity]:
        """Match congress trading signals to Polymarket markets.

        Logic:
        - Congress BUY on NVDA + Polymarket "NVDA above $X" = BUY_YES
        - Congress SELL on NVDA + Polymarket "NVDA above $X" = BUY_NO
        - Congress cluster on defense stock + Polymarket legislation market = flag
        """
        matches = []
        ticker = signal.ticker.upper()
        company = TICKER_TO_COMPANY.get(ticker, ticker)

        # Search terms: ticker and company name
        search_terms = [ticker.lower()]
        if company != ticker:
            search_terms.append(company.lower())

        markets = self._get_cached_markets()

        for market in markets:
            question = (market.get("question", "") or "").lower()
            desc = (market.get("description", "") or "").lower()
            full_text = f"{question} {desc}"

            # Check if ticker or company appears
            ticker_match = ticker.lower() in full_text
            company_match = company.lower() in full_text if company != ticker else False

            if not (ticker_match or company_match):
                continue

            yes_price = self._get_market_yes_price(market)
            volume = float(market.get("volume", 0) or 0)

            # Skip low-volume markets
            if volume < 5000:
                continue

            # Determine suggested side
            price_info = self._parse_price_target(market.get("question", ""))

            if signal.direction == "BULLISH":
                suggested_side = "BUY_YES"
                # Edge estimate: strength-weighted, capped
                # If market already priced high, less edge
                raw_edge = signal.strength * 0.15 * (1 - yes_price)
            else:  # BEARISH
                suggested_side = "BUY_NO"
                raw_edge = signal.strength * 0.15 * yes_price

            # Adjust for price target direction
            if price_info:
                _, direction, _ = price_info
                if signal.direction == "BULLISH" and direction == "above":
                    raw_edge *= 1.3  # signal aligns with market direction
                elif signal.direction == "BEARISH" and direction == "below":
                    raw_edge *= 1.3
                elif signal.direction == "BEARISH" and direction == "above":
                    suggested_side = "BUY_NO"
                    raw_edge *= 1.2

            # Confidence: based on match quality and signal strength
            confidence = 0.0
            if ticker_match:
                confidence += 0.5
            if company_match:
                confidence += 0.3
            confidence += signal.strength * 0.2

            # Congress signals with 3+ members are stronger
            n_members = 0
            member_match = re.search(r'(\d+)\s+congress\s+members', signal.detail)
            if member_match:
                n_members = int(member_match.group(1))
                if n_members >= 5:
                    confidence = min(1.0, confidence + 0.2)
                    raw_edge *= 1.3

            # Conservative: only flag if confidence >= 0.5 and edge >= 0.03
            if confidence >= 0.5 and raw_edge >= 0.03:
                matches.append(MatchedOpportunity(
                    signal_source="congress",
                    signal_ticker=ticker,
                    signal_direction=signal.direction,
                    signal_strength=signal.strength,
                    signal_detail=signal.detail,
                    market_id=market.get("id", ""),
                    market_question=market.get("question", ""),
                    market_slug=market.get("slug", ""),
                    market_yes_price=yes_price,
                    market_volume=volume,
                    suggested_side=suggested_side,
                    edge_estimate=round(min(raw_edge, 0.25), 4),
                    confidence=round(confidence, 3),
                    match_reason=f"Congress {signal.direction.lower()} on {ticker}"
                                 f" ({company}) — {n_members} members"
                                 f" | market YES@{yes_price:.2f}",
                ))

        return matches

    def match_crypto_signal(self, signal: UWSignal) -> list[MatchedOpportunity]:
        """Match crypto whale signals to Polymarket crypto price markets.

        Logic:
        - Whale sells $4M ETH + "ETH above $2500" = BUY_NO
        - Whale buys $10M BTC + "BTC above $100K" = BUY_YES
        """
        matches = []
        ticker = signal.ticker.upper()

        # Map to crypto names
        crypto_names = CRYPTO_TICKERS.get(ticker)
        if not crypto_names:
            # Try reverse: maybe ticker is company name with crypto exposure
            return matches

        markets = self._get_cached_markets()

        for market in markets:
            question = (market.get("question", "") or "").lower()
            desc = (market.get("description", "") or "").lower()
            full_text = f"{question} {desc}"

            # Check if any crypto name appears
            name_matched = False
            for name in crypto_names:
                if name.lower() in full_text:
                    name_matched = True
                    break

            if not name_matched:
                continue

            # Must be a price-related market
            price_keywords = ["above", "below", "price", "reach", "hit", "exceed",
                              "drop", "fall", "rise", "close"]
            has_price = any(kw in question for kw in price_keywords)
            if not has_price:
                continue

            yes_price = self._get_market_yes_price(market)
            volume = float(market.get("volume", 0) or 0)

            if volume < 5000:
                continue

            price_info = self._parse_price_target(market.get("question", ""))

            if signal.direction == "BULLISH":
                suggested_side = "BUY_YES"
                raw_edge = signal.strength * 0.12 * (1 - yes_price)
            else:
                suggested_side = "BUY_NO"
                raw_edge = signal.strength * 0.12 * yes_price

            # Align side with market direction
            if price_info:
                _, direction, _ = price_info
                if signal.direction == "BEARISH" and direction == "above":
                    suggested_side = "BUY_NO"
                elif signal.direction == "BULLISH" and direction == "below":
                    suggested_side = "BUY_NO"

            confidence = 0.5 + signal.strength * 0.3

            # Whale size matters — larger trades = more conviction
            size_match = re.search(r'\$([\d,.]+)\s*[MBmb]', signal.detail)
            if size_match:
                size_val = float(size_match.group(1).replace(",", ""))
                if size_val >= 10:  # $10M+
                    confidence = min(1.0, confidence + 0.15)
                    raw_edge *= 1.3
                elif size_val >= 5:
                    confidence = min(1.0, confidence + 0.1)
                    raw_edge *= 1.15

            if confidence >= 0.5 and raw_edge >= 0.03:
                matches.append(MatchedOpportunity(
                    signal_source="crypto_whale",
                    signal_ticker=ticker,
                    signal_direction=signal.direction,
                    signal_strength=signal.strength,
                    signal_detail=signal.detail,
                    market_id=market.get("id", ""),
                    market_question=market.get("question", ""),
                    market_slug=market.get("slug", ""),
                    market_yes_price=yes_price,
                    market_volume=volume,
                    suggested_side=suggested_side,
                    edge_estimate=round(min(raw_edge, 0.20), 4),
                    confidence=round(confidence, 3),
                    match_reason=f"Crypto whale {signal.direction.lower()} on {ticker}"
                                 f" | market YES@{yes_price:.2f}",
                ))

        return matches

    def match_econ_signal(self, signal: UWSignal) -> list[MatchedOpportunity]:
        """Match economic calendar events to Polymarket markets.

        Logic:
        - FOMC upcoming + "Fed rate cut" market = flag for econ_sniper
        - CPI release + "inflation above X%" = directional signal
        """
        matches = []
        detail_lower = signal.detail.lower()

        # Identify which economic event this is
        matched_event = None
        search_terms = []
        for event_key, terms in ECON_KEYWORDS.items():
            if any(t.lower() in detail_lower for t in terms):
                matched_event = event_key
                search_terms = [t.lower() for t in terms]
                break

        if not matched_event:
            return matches

        markets = self._get_cached_markets()

        for market in markets:
            question = (market.get("question", "") or "").lower()
            desc = (market.get("description", "") or "").lower()
            full_text = f"{question} {desc}"

            # Need at least 1 search term to match
            term_matches = sum(1 for t in search_terms if t in full_text)
            if term_matches == 0:
                continue

            yes_price = self._get_market_yes_price(market)
            volume = float(market.get("volume", 0) or 0)

            if volume < 10000:
                continue

            # Economic events are harder to directionally predict
            # Flag for econ_sniper review rather than direct trade
            confidence = min(0.8, 0.3 + term_matches * 0.15 + signal.strength * 0.2)

            # Conservative edge: econ events are well-priced
            raw_edge = signal.strength * 0.08

            # Direction depends on event type and consensus
            suggested_side = "WATCH"  # default: flag for review
            if signal.direction == "BULLISH":
                if any(kw in full_text for kw in ["rate cut", "lower rate"]):
                    suggested_side = "BUY_YES"
                    raw_edge *= 1.2
                elif any(kw in full_text for kw in ["rate hike", "raise rate"]):
                    suggested_side = "BUY_NO"
            elif signal.direction == "BEARISH":
                if any(kw in full_text for kw in ["recession", "contraction"]):
                    suggested_side = "BUY_YES"
                    raw_edge *= 1.1

            if confidence >= 0.4 and raw_edge >= 0.02:
                matches.append(MatchedOpportunity(
                    signal_source="econ_calendar",
                    signal_ticker=matched_event,
                    signal_direction=signal.direction,
                    signal_strength=signal.strength,
                    signal_detail=signal.detail,
                    market_id=market.get("id", ""),
                    market_question=market.get("question", ""),
                    market_slug=market.get("slug", ""),
                    market_yes_price=yes_price,
                    market_volume=volume,
                    suggested_side=suggested_side,
                    edge_estimate=round(min(raw_edge, 0.15), 4),
                    confidence=round(confidence, 3),
                    match_reason=f"Econ event {matched_event} → {term_matches} keyword matches"
                                 f" | market YES@{yes_price:.2f}",
                ))

        return matches

    def match_insider_signal(self, signal: UWSignal) -> list[MatchedOpportunity]:
        """Match insider trading signals to Polymarket markets.

        Logic:
        - Multiple insider buys on TSLA + "TSLA earnings beat" = BUY_YES
        - Insider sells on AAPL + "AAPL above $200" = BUY_NO
        """
        matches = []
        ticker = signal.ticker.upper()
        company = TICKER_TO_COMPANY.get(ticker, ticker)

        markets = self._get_cached_markets()

        for market in markets:
            question = (market.get("question", "") or "").lower()
            desc = (market.get("description", "") or "").lower()
            full_text = f"{question} {desc}"

            ticker_match = ticker.lower() in full_text
            company_match = company.lower() in full_text if company != ticker else False

            if not (ticker_match or company_match):
                continue

            yes_price = self._get_market_yes_price(market)
            volume = float(market.get("volume", 0) or 0)

            if volume < 5000:
                continue

            # Insider buys near earnings are strongest signal
            is_earnings = any(kw in full_text for kw in ["earnings", "revenue", "beat", "miss",
                                                          "quarterly", "profit", "EPS"])

            if signal.direction == "BULLISH":
                suggested_side = "BUY_YES"
                raw_edge = signal.strength * 0.12 * (1 - yes_price)
                if is_earnings:
                    raw_edge *= 1.5  # insider buys before earnings = strong
            else:
                suggested_side = "BUY_NO"
                raw_edge = signal.strength * 0.10 * yes_price

            confidence = 0.0
            if ticker_match:
                confidence += 0.4
            if company_match:
                confidence += 0.2
            if is_earnings:
                confidence += 0.2
            confidence += signal.strength * 0.2

            # Insider cluster size
            n_buys_match = re.search(r'(\d+)\s+insider\s+buy', signal.detail)
            if n_buys_match:
                n_buys = int(n_buys_match.group(1))
                if n_buys >= 4:
                    confidence = min(1.0, confidence + 0.15)
                    raw_edge *= 1.3

            if confidence >= 0.5 and raw_edge >= 0.03:
                matches.append(MatchedOpportunity(
                    signal_source="insider",
                    signal_ticker=ticker,
                    signal_direction=signal.direction,
                    signal_strength=signal.strength,
                    signal_detail=signal.detail,
                    market_id=market.get("id", ""),
                    market_question=market.get("question", ""),
                    market_slug=market.get("slug", ""),
                    market_yes_price=yes_price,
                    market_volume=volume,
                    suggested_side=suggested_side,
                    edge_estimate=round(min(raw_edge, 0.20), 4),
                    confidence=round(confidence, 3),
                    match_reason=f"Insider {signal.direction.lower()} on {ticker}"
                                 f" ({company}) | earnings={is_earnings}"
                                 f" | market YES@{yes_price:.2f}",
                ))

        return matches

    def match_darkpool_signal(self, signal: UWSignal) -> list[MatchedOpportunity]:
        """Match dark pool signals. Same logic as congress but weaker edge."""
        matches = []
        ticker = signal.ticker.upper()
        company = TICKER_TO_COMPANY.get(ticker, ticker)

        markets = self._get_cached_markets()

        for market in markets:
            question = (market.get("question", "") or "").lower()
            desc = (market.get("description", "") or "").lower()
            full_text = f"{question} {desc}"

            ticker_match = ticker.lower() in full_text
            company_match = company.lower() in full_text if company != ticker else False

            if not (ticker_match or company_match):
                continue

            yes_price = self._get_market_yes_price(market)
            volume = float(market.get("volume", 0) or 0)

            if volume < 10000:
                continue  # dark pool needs higher volume threshold

            if signal.direction == "BULLISH":
                suggested_side = "BUY_YES"
                raw_edge = signal.strength * 0.08 * (1 - yes_price)
            else:
                suggested_side = "BUY_NO"
                raw_edge = signal.strength * 0.08 * yes_price

            confidence = 0.0
            if ticker_match:
                confidence += 0.4
            if company_match:
                confidence += 0.2
            confidence += signal.strength * 0.2

            # Dark pool is noisier, require higher thresholds
            if confidence >= 0.55 and raw_edge >= 0.04:
                matches.append(MatchedOpportunity(
                    signal_source="darkpool",
                    signal_ticker=ticker,
                    signal_direction=signal.direction,
                    signal_strength=signal.strength,
                    signal_detail=signal.detail,
                    market_id=market.get("id", ""),
                    market_question=market.get("question", ""),
                    market_slug=market.get("slug", ""),
                    market_yes_price=yes_price,
                    market_volume=volume,
                    suggested_side=suggested_side,
                    edge_estimate=round(min(raw_edge, 0.15), 4),
                    confidence=round(confidence, 3),
                    match_reason=f"Dark pool {signal.direction.lower()} on {ticker}"
                                 f" ({company}) | market YES@{yes_price:.2f}",
                ))

        return matches

    # ── Main Entry Point ─────────────────────────────────────

    def match_signals(self, signals: list[UWSignal]) -> list[MatchedOpportunity]:
        """Match a list of UW signals to Polymarket markets.

        Returns opportunities sorted by edge_estimate descending.
        Only returns matches with edge >= 5% (high confidence).
        """
        if not signals:
            return []

        all_matches = []

        for signal in signals:
            try:
                if signal.source == "congress":
                    all_matches.extend(self.match_congress_signal(signal))
                elif signal.source == "darkpool":
                    all_matches.extend(self.match_darkpool_signal(signal))
                elif signal.source == "insider":
                    all_matches.extend(self.match_insider_signal(signal))
                elif signal.source in ("options_flow", "crypto_whale"):
                    # Check if it's a crypto ticker
                    if signal.ticker.upper() in CRYPTO_TICKERS:
                        all_matches.extend(self.match_crypto_signal(signal))
                    else:
                        # Treat like insider/congress
                        all_matches.extend(self.match_congress_signal(signal))
            except Exception as e:
                logger.debug(f"[UW-MATCH] Error matching {signal.source}/{signal.ticker}: {e}")

        # Also try economic signals from any source mentioning econ keywords
        for signal in signals:
            try:
                econ_matches = self.match_econ_signal(signal)
                all_matches.extend(econ_matches)
            except Exception:
                pass

        # Deduplicate by market_id
        seen = set()
        unique = []
        for m in all_matches:
            if m.market_id not in seen:
                seen.add(m.market_id)
                unique.append(m)

        # Sort by edge descending
        unique.sort(key=lambda x: x.edge_estimate, reverse=True)

        # Save to history
        if unique:
            for m in unique:
                self._match_history.append(asdict(m))
            self._match_history = self._match_history[-200:]
            try:
                MATCHES_FILE.write_text(json.dumps(self._match_history, indent=2))
            except Exception:
                pass

        logger.info(
            f"[UW-MATCH] {len(signals)} signals → {len(unique)} matches "
            f"(edge>=5%: {sum(1 for m in unique if m.edge_estimate >= 0.05)})"
        )

        return unique

    def get_actionable(self, signals: list[UWSignal], min_edge: float = 0.05) -> list[MatchedOpportunity]:
        """Get only actionable matches with edge >= min_edge.

        These are high-confidence matches that could be traded.
        """
        all_matches = self.match_signals(signals)
        actionable = [m for m in all_matches if m.edge_estimate >= min_edge and m.suggested_side != "WATCH"]
        if actionable:
            logger.info(
                f"[UW-MATCH] {len(actionable)} actionable opportunities "
                f"(edge>={min_edge:.0%}): "
                + ", ".join(
                    f"{m.signal_ticker}→{m.suggested_side}@{m.edge_estimate:.1%}"
                    for m in actionable[:5]
                )
            )
        return actionable

    def status(self) -> dict:
        """Return matcher status."""
        return {
            "cached_markets": len(self._market_cache),
            "cache_age_min": round((time.time() - self._cache_ts) / 60, 1) if self._cache_ts else None,
            "match_history": len(self._match_history),
        }
