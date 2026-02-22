"""
Attribution Engine v9.0 — P&L per segnale con Brier score.

Traccia ogni trade dall'entry all'exit, calcolando:
- PnL per segnale/strategia/categoria
- Brier score per misurare la calibrazione
- Alpha decay per detectare strategie che perdono edge
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SignalAttribution:
    """Record di un singolo trade per attribution."""
    trade_id: str           # token_id o market_id
    strategy: str
    signal_type: str        # news_reactive, structural, bond, whale_copy, etc.
    category: str           # politics, crypto, weather, etc.
    entry_time: float       # timestamp entry
    exit_time: float = 0.0
    pnl: float = 0.0
    edge_predicted: float = 0.0
    edge_realized: float = 0.0
    validation_score: float = 0.0
    brier_score: float = 0.0
    win_prob_predicted: float = 0.0
    won: bool = False


class AttributionEngine:
    """Motore di attribution per tracciare performance per segnale."""

    def __init__(self, db=None):
        self.db = db
        self._active: dict[str, SignalAttribution] = {}  # trade_id -> attribution
        self._completed: list[SignalAttribution] = []
        self._max_completed = 5000  # rolling window

    def record_entry(self, trade_id: str, strategy: str,
                     signal_type: str = "", category: str = "",
                     edge_predicted: float = 0.0,
                     validation_score: float = 0.0,
                     win_prob_predicted: float = 0.0):
        """Registra l'apertura di un trade."""
        attr = SignalAttribution(
            trade_id=trade_id,
            strategy=strategy,
            signal_type=signal_type,
            category=category,
            entry_time=time.time(),
            edge_predicted=edge_predicted,
            validation_score=validation_score,
            win_prob_predicted=win_prob_predicted,
        )
        self._active[trade_id] = attr
        logger.debug(
            f"[ATTRIBUTION] Entry: {strategy}/{signal_type} "
            f"edge={edge_predicted:.4f} score={validation_score:.2f}"
        )

    def record_exit(self, trade_id: str, pnl: float, won: bool,
                    win_prob_predicted: float = 0.0):
        """
        Registra la chiusura di un trade e calcola Brier score.

        Brier score: (predicted_prob - outcome)^2
        - 0.0 = calibrazione perfetta
        - 0.25 = random (50/50 su tutto)
        - 1.0 = perfettamente sbagliato
        """
        attr = self._active.pop(trade_id, None)
        if attr is None:
            # Trade non tracciato (aperto prima dell'attribution engine)
            attr = SignalAttribution(
                trade_id=trade_id,
                strategy="unknown",
                signal_type="unknown",
                category="unknown",
                entry_time=0,
            )

        attr.exit_time = time.time()
        attr.pnl = pnl
        attr.won = won

        # Usa win_prob_predicted passato o quello dell'entry
        prob = win_prob_predicted or attr.win_prob_predicted
        if prob > 0:
            outcome = 1.0 if won else 0.0
            attr.brier_score = (prob - outcome) ** 2
        else:
            attr.brier_score = 0.25  # default: come random

        # Calcola edge realizzato
        if attr.entry_time > 0:
            holding_hours = (attr.exit_time - attr.entry_time) / 3600
        else:
            holding_hours = 0
        attr.edge_realized = pnl  # PnL assoluto come proxy

        self._completed.append(attr)
        # Rolling window
        if len(self._completed) > self._max_completed:
            self._completed = self._completed[-self._max_completed:]

        logger.debug(
            f"[ATTRIBUTION] Exit: {attr.strategy}/{attr.signal_type} "
            f"PnL=${pnl:+.2f} won={won} brier={attr.brier_score:.4f}"
        )

    def get_brier_score(self, signal_type: str = "",
                        strategy: str = "", window: int = 50) -> float:
        """
        Calcola Brier score medio per signal_type o strategia.

        Returns: 0.0 (perfetto) - 1.0 (terribile). Default 0.25 (random).
        """
        relevant = self._completed
        if signal_type:
            relevant = [a for a in relevant if a.signal_type == signal_type]
        if strategy:
            relevant = [a for a in relevant if a.strategy == strategy]

        relevant = relevant[-window:]
        if not relevant:
            return 0.25  # nessun dato = come random

        return sum(a.brier_score for a in relevant) / len(relevant)

    def get_alpha_decay(self, strategy: str,
                        signal_type: str = "",
                        window: int = 50) -> float:
        """
        Misura se l'alpha di una strategia sta calando.

        Confronta win rate recente (ultimi window/2) vs storico (window completo).
        Returns: >1.0 = alpha in crescita, <1.0 = alpha in calo.
        """
        relevant = [
            a for a in self._completed
            if a.strategy == strategy
        ]
        if signal_type:
            relevant = [a for a in relevant if a.signal_type == signal_type]

        relevant = relevant[-window:]
        if len(relevant) < 10:
            return 1.0  # non abbastanza dati

        half = len(relevant) // 2
        old = relevant[:half]
        recent = relevant[half:]

        old_wr = sum(1 for a in old if a.won) / len(old) if old else 0.5
        recent_wr = sum(1 for a in recent if a.won) / len(recent) if recent else 0.5

        if old_wr <= 0:
            return 1.0
        return recent_wr / old_wr

    @property
    def report(self) -> dict:
        """Report completo di attribution."""
        pnl_by_signal: dict[str, float] = defaultdict(float)
        pnl_by_category: dict[str, float] = defaultdict(float)
        pnl_by_strategy: dict[str, float] = defaultdict(float)
        count_by_signal: dict[str, int] = defaultdict(int)

        for a in self._completed:
            pnl_by_signal[a.signal_type] += a.pnl
            pnl_by_category[a.category] += a.pnl
            pnl_by_strategy[a.strategy] += a.pnl
            count_by_signal[a.signal_type] += 1

        # Brier scores per strategia
        strategies = set(a.strategy for a in self._completed)
        brier_scores = {s: self.get_brier_score(strategy=s) for s in strategies}

        return {
            "pnl_by_signal": dict(pnl_by_signal),
            "pnl_by_category": dict(pnl_by_category),
            "pnl_by_strategy": dict(pnl_by_strategy),
            "count_by_signal": dict(count_by_signal),
            "brier_scores": brier_scores,
            "total_tracked": len(self._completed),
            "active_trades": len(self._active),
        }
