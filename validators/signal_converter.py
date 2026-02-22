"""
Signal Converter v9.0 — Adattatori strategia → UnifiedSignal.

Converte le strutture dati di ogni strategia in un formato normalizzato
per il SignalValidator.
"""

import logging
from datetime import datetime, timezone
from validators.signal_validator import UnifiedSignal

logger = logging.getLogger(__name__)

def _days_until(end_date_str: str) -> float:
    """Calcola giorni fino a end_date. Ritorna -1 se non parsabile."""
    if not end_date_str:
        return -1.0
    try:
        # Prova formato ISO
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (end - now).total_seconds() / 86400
        return max(0.0, delta)
    except (ValueError, TypeError):
        return -1.0


def from_event_opportunity(opp, market=None) -> UnifiedSignal:
    """EventOpportunity → UnifiedSignal."""
    m = market or opp.market
    side = opp.side.upper()
    price = m.prices.get(side.lower(), m.prices.get("yes", 0.5))

    return UnifiedSignal(
        strategy="event_driven",
        market_id=m.id,
        question=m.question,
        side=side,
        price=price,
        edge=opp.edge,
        confidence=opp.confidence,
        signal_type=getattr(opp, 'signal_type', 'structural'),
        category=getattr(opp, 'event_type', m.category),
        volume=m.volume,
        liquidity=m.liquidity,
        spread=m.spread,
        news_strength=getattr(opp, 'news_strength', 0.0),
        days_to_resolution=_days_until(m.end_date),
        reasoning=opp.reasoning,
    )


def from_bond_opportunity(opp) -> UnifiedSignal:
    """BondOpportunity → UnifiedSignal."""
    m = opp.market
    return UnifiedSignal(
        strategy="high_prob_bond",
        market_id=m.id,
        question=m.question,
        side="YES",  # Bond compra sempre YES
        price=opp.price_yes,
        edge=opp.edge,
        confidence=opp.certainty_score,
        signal_type="bond",
        category=m.category,
        volume=m.volume,
        liquidity=m.liquidity,
        spread=m.spread,
        days_to_resolution=opp.days_to_resolution,
        reasoning=opp.reasoning,
    )


def from_whale_opportunity(opp) -> UnifiedSignal:
    """WhaleCopyOpportunity → UnifiedSignal."""
    m = opp.market
    side = opp.side.upper()
    price = m.prices.get(side.lower(), m.prices.get("yes", 0.5))

    return UnifiedSignal(
        strategy="whale_copy",
        market_id=m.id,
        question=m.question,
        side=side,
        price=price,
        edge=opp.edge,
        confidence=opp.confidence,
        signal_type="whale_copy",
        category=m.category,
        volume=m.volume,
        liquidity=m.liquidity,
        spread=m.spread,
        whale_consensus=opp.confidence,
        days_to_resolution=_days_until(m.end_date),
        reasoning=opp.reasoning,
        kelly_size=opp.copy_size,
    )


def from_prediction(pred) -> UnifiedSignal:
    """Prediction (data_driven) → UnifiedSignal."""
    m = pred.market
    side = pred.best_side.upper()
    price = m.prices.get(side.lower(), m.prices.get("yes", 0.5))

    return UnifiedSignal(
        strategy="data_driven",
        market_id=m.id,
        question=m.question,
        side=side,
        price=price,
        edge=pred.best_edge,
        confidence=pred.confidence,
        signal_type="data_driven",
        category=m.category,
        volume=m.volume,
        liquidity=m.liquidity,
        spread=m.spread,
        days_to_resolution=_days_until(m.end_date),
        reasoning=pred.reasoning,
    )


def from_weather_opportunity(opp) -> UnifiedSignal:
    """WeatherOpportunity → UnifiedSignal."""
    m = opp.market
    side = opp.side.upper()
    price = m.prices.get(side.lower(), m.prices.get("yes", 0.5))

    return UnifiedSignal(
        strategy="weather",
        market_id=m.id,
        question=m.question,
        side=side,
        price=price,
        edge=opp.edge,
        confidence=opp.confidence,
        signal_type="weather",
        category="weather",
        volume=m.volume,
        liquidity=m.liquidity,
        spread=m.spread,
        days_to_resolution=_days_until(m.end_date),
        reasoning=opp.reasoning,
    )
