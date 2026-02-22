"""
Orchestrator Agent v9.0 — Prioritizzazione e routing mercati.

Classifica ogni mercato per priorita' e instrada alle strategie piu' adatte.
Riduce latenza: mercati ad alta priorita' scansionati prima,
mercati dormienti saltati.

Priorita':
- CRITICAL: Breaking news, arb opportunity, volume spike >3x
- HIGH: News-reactive, whale trade, prezzo >0.93 o <0.07
- MEDIUM: Structural signal, bond candidate, volume >50K + spread <2%
- LOW: Routine scan
- SKIP: Dormiente, no volume (<100)
"""

import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)


class MarketPriority(IntEnum):
    CRITICAL = 0    # Breaking news, arb opportunity
    HIGH = 1        # News-reactive, whale trade
    MEDIUM = 2      # Structural signal, bond
    LOW = 3         # Routine scan
    SKIP = 4        # Dormiente, no volume


@dataclass
class MarketTask:
    """Un mercato con priorita' e strategie assegnate."""
    market_id: str
    priority: MarketPriority
    strategies: list[str] = field(default_factory=list)
    anomaly_score: float = 0.0
    reason: str = ""


class OrchestratorAgent:
    """Classifica mercati per priorita' e routing a strategie."""

    # Soglie per classificazione
    VOLUME_SPIKE_MULTIPLIER = 3.0   # Volume >3x media = CRITICAL
    HIGH_PRICE_THRESHOLD = 0.93     # Prezzo YES >0.93 = bond candidate (HIGH)
    LOW_PRICE_THRESHOLD = 0.07      # Prezzo YES <0.07 = near-zero (HIGH)
    MEDIUM_VOLUME_THRESHOLD = 50000 # Volume >50K = MEDIUM
    MEDIUM_SPREAD_THRESHOLD = 0.02  # Spread <2% = MEDIUM
    SKIP_VOLUME_THRESHOLD = 100     # Volume <100 = SKIP

    def __init__(self, event_bus=None):
        self.event_bus = event_bus
        self._volume_history: dict[str, list[float]] = {}  # market_id -> [volumes]
        self._max_history = 50

    async def prioritize(self, markets: list) -> list[MarketTask]:
        """
        Classifica tutti i mercati per priorita'.
        Ritorna lista ordinata (CRITICAL prima, SKIP ultimi).
        """
        tasks = []
        skipped = 0

        for market in markets:
            priority = self._classify_priority(market)

            if priority == MarketPriority.SKIP:
                skipped += 1
                continue

            strategies = self._route_strategies(market, priority)
            anomaly = self._anomaly_score(market)

            task = MarketTask(
                market_id=market.id,
                priority=priority,
                strategies=strategies,
                anomaly_score=anomaly,
            )
            tasks.append(task)

            # Aggiorna history volume
            self._update_volume_history(market.id, market.volume)

        # Ordina per priorita' (CRITICAL=0 prima)
        tasks.sort(key=lambda t: (t.priority, -t.anomaly_score))

        if tasks:
            critical = sum(1 for t in tasks if t.priority == MarketPriority.CRITICAL)
            high = sum(1 for t in tasks if t.priority == MarketPriority.HIGH)
            logger.debug(
                f"[ORCHESTRATOR] {len(tasks)} mercati prioritizzati "
                f"(CRITICAL={critical} HIGH={high} SKIP={skipped})"
            )

        return tasks

    def _classify_priority(self, market) -> MarketPriority:
        """Classifica la priorita' di un singolo mercato."""
        volume = getattr(market, 'volume', 0) or 0
        liquidity = getattr(market, 'liquidity', 0) or 0
        prices = getattr(market, 'prices', {}) or {}
        price_yes = prices.get("yes", 0.5)
        spread = getattr(market, 'spread', 0.05)

        # SKIP: volume troppo basso
        if volume < self.SKIP_VOLUME_THRESHOLD:
            return MarketPriority.SKIP

        # CRITICAL: volume spike
        avg_volume = self._avg_volume(market.id)
        if avg_volume > 0 and volume > avg_volume * self.VOLUME_SPIKE_MULTIPLIER:
            return MarketPriority.CRITICAL

        # HIGH: prezzo estremo (bond candidate o near-zero)
        if price_yes > self.HIGH_PRICE_THRESHOLD or price_yes < self.LOW_PRICE_THRESHOLD:
            return MarketPriority.HIGH

        # MEDIUM: buon volume + spread stretto
        if volume > self.MEDIUM_VOLUME_THRESHOLD and spread < self.MEDIUM_SPREAD_THRESHOLD:
            return MarketPriority.MEDIUM

        # LOW: tutto il resto
        return MarketPriority.LOW

    def _route_strategies(self, market, priority: MarketPriority) -> list[str]:
        """Determina quali strategie devono analizzare questo mercato."""
        if priority in (MarketPriority.CRITICAL, MarketPriority.HIGH):
            # Tutte le strategie
            return [
                "high_prob_bond", "event_driven", "weather",
                "data_driven", "whale_copy", "arb_gabagool", "arbitrage",
            ]
        elif priority == MarketPriority.MEDIUM:
            return ["high_prob_bond", "data_driven", "event_driven"]
        else:
            return ["data_driven"]

    def _anomaly_score(self, market) -> float:
        """
        Calcola un anomaly score (0-1) basato su deviazioni dalla norma.
        Piu' alto = piu' anomalo = piu' interessante.
        """
        score = 0.0
        volume = getattr(market, 'volume', 0) or 0
        spread = getattr(market, 'spread', 0.05)
        mispricing = getattr(market, 'mispricing_score', 0)

        # Volume spike
        avg_volume = self._avg_volume(market.id)
        if avg_volume > 0:
            vol_ratio = volume / avg_volume
            if vol_ratio > 2.0:
                score += min(0.4, (vol_ratio - 1.0) * 0.1)

        # Spread anomalo (troppo alto = market inefficiente)
        if spread > 0.03:
            score += min(0.2, spread * 2.0)

        # Mispricing
        if mispricing > 0.01:
            score += min(0.4, mispricing * 4.0)

        return min(1.0, score)

    def _avg_volume(self, market_id: str) -> float:
        """Media volume storica per un mercato."""
        history = self._volume_history.get(market_id, [])
        if not history:
            return 0.0
        return sum(history) / len(history)

    def _update_volume_history(self, market_id: str, volume: float):
        """Aggiorna la storia volume per un mercato."""
        if market_id not in self._volume_history:
            self._volume_history[market_id] = []
        self._volume_history[market_id].append(volume)
        if len(self._volume_history[market_id]) > self._max_history:
            self._volume_history[market_id] = \
                self._volume_history[market_id][-self._max_history:]

    def get_market_priority(self, market_id: str) -> MarketPriority | None:
        """Ritorna la priorita' di un mercato specifico (se noto)."""
        # Usato per query puntuale
        return None  # richiede re-classificazione

    def stats(self) -> dict:
        """Statistiche dell'orchestrator."""
        return {
            "tracked_markets": len(self._volume_history),
        }
