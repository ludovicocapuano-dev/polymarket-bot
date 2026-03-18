"""
Market Data Models — Normalized representations for Polymarket data (v12.8)
============================================================================
Clean dataclasses for markets, tokens, prices.
MarketNormalizer converts raw Gamma API responses into typed objects.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OutcomeToken:
    """A single outcome in a market (YES or NO token)."""
    token_id: str
    outcome: str  # "Yes" or "No"
    price: float
    winner: Optional[bool] = None


@dataclass
class Market:
    """Normalized market representation."""
    market_id: str
    condition_id: str
    question: str
    description: str
    category: str
    created_at: datetime
    end_date_iso: Optional[datetime]
    active: bool
    closed: bool
    archived: bool
    resolved: bool
    tokens: List[OutcomeToken] = field(default_factory=list)
    volume: float = 0.0
    liquidity: float = 0.0
    resolution_source: str = ""
    winner: Optional[str] = None
    neg_risk: bool = False

    @property
    def yes_price(self) -> Optional[float]:
        for token in self.tokens:
            if token.outcome and token.outcome.lower() == "yes":
                return token.price
        return None

    @property
    def no_price(self) -> Optional[float]:
        for token in self.tokens:
            if token.outcome and token.outcome.lower() == "no":
                return token.price
        return None

    @property
    def implied_probability(self) -> Optional[float]:
        return self.yes_price

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "category": self.category,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "volume": self.volume,
            "liquidity": self.liquidity,
            "active": self.active,
            "resolved": self.resolved,
            "end_date": self.end_date_iso.isoformat() if self.end_date_iso else None,
        }


class MarketNormalizer:
    """Converts raw API responses into clean Market objects."""

    def normalize_market(self, raw: dict) -> Optional[Market]:
        try:
            tokens = self._extract_tokens(raw)
            created_at = self._parse_timestamp(raw.get("createdAt", ""))
            end_date = self._parse_timestamp(raw.get("endDateIso", raw.get("endDate", "")))

            return Market(
                market_id=str(raw.get("id", raw.get("conditionId", ""))),
                condition_id=str(raw.get("conditionId", "")),
                question=raw.get("question", ""),
                description=raw.get("description", ""),
                category=raw.get("category", ""),
                created_at=created_at or datetime.utcnow(),
                end_date_iso=end_date,
                active=bool(raw.get("active", False)),
                closed=bool(raw.get("closed", False)),
                archived=bool(raw.get("archived", False)),
                resolved=bool(raw.get("resolved", False)),
                tokens=tokens,
                volume=float(raw.get("volume", 0) or 0),
                liquidity=float(raw.get("liquidity", 0) or 0),
                resolution_source=raw.get("resolutionSource", ""),
                winner=raw.get("winner"),
                neg_risk=bool(raw.get("negRisk", False)),
            )
        except Exception as e:
            logger.debug(f"Normalize error {raw.get('id', '?')}: {e}")
            return None

    def _extract_tokens(self, raw: dict) -> List[OutcomeToken]:
        tokens = []

        # Try structured tokens field first
        token_data = raw.get("tokens") or []
        if token_data:
            for t in token_data:
                try:
                    tokens.append(OutcomeToken(
                        token_id=str(t.get("token_id", t.get("id", ""))),
                        outcome=str(t.get("outcome", "")),
                        price=float(t.get("price", 0.5) or 0.5),
                        winner=t.get("winner"),
                    ))
                except Exception:
                    continue
            return tokens

        # Fallback: clobTokenIds + outcomePrices
        import json
        clob_ids = raw.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except Exception:
                clob_ids = []

        outcomes = raw.get("outcomes", ["Yes", "No"])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = ["Yes", "No"]

        prices = raw.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = []

        for i, tid in enumerate(clob_ids):
            outcome = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
            price = float(prices[i]) if i < len(prices) else 0.5
            tokens.append(OutcomeToken(token_id=str(tid), outcome=outcome, price=price))

        return tokens

    def _parse_timestamp(self, ts: str) -> Optional[datetime]:
        if not ts:
            return None
        for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                     "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
        return None

    def normalize_batch(self, raw_markets: list) -> List[Market]:
        normalized = []
        for raw in raw_markets:
            m = self.normalize_market(raw)
            if m and m.market_id:
                normalized.append(m)
        return normalized


normalizer = MarketNormalizer()
