"""
Attribution Engine v11.0 — P&L attribution + IC + Brier decomposition.

v9.0: Basic Brier score + alpha decay
v11.0 (Qlib-inspired):
  - Information Coefficient (IC): rank correlation between predicted edge
    and realized outcome. IC > 0.05 = useful signal (Qlib standard).
  - Brier Decomposition (Murphy 1973):
    Brier = Reliability - Resolution + Uncertainty
    - Reliability: how well-calibrated are the probabilities
    - Resolution: how much the probabilities vary from base rate
    - Uncertainty: inherent unpredictability (base_rate * (1 - base_rate))
  - IC Decay: rolling IC over time windows to detect signal staleness
"""

import logging
import math
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

    def get_information_coefficient(self, strategy: str = "",
                                     window: int = 50) -> float:
        """
        Information Coefficient (IC) — Qlib's primary signal quality metric.

        IC = Spearman rank correlation between predicted edge and realized outcome.
        IC > 0.05 = useful signal, IC > 0.10 = strong signal.
        IC ≈ 0 = random, IC < 0 = contrarian (invert signal).

        Uses rank correlation (Spearman) to be robust to outliers.
        """
        relevant = self._completed
        if strategy:
            relevant = [a for a in relevant if a.strategy == strategy]
        relevant = relevant[-window:]

        # Need predicted prob and outcome
        pairs = [
            (a.win_prob_predicted or a.edge_predicted, 1.0 if a.won else 0.0)
            for a in relevant
            if (a.win_prob_predicted or a.edge_predicted) > 0
        ]

        if len(pairs) < 10:
            return 0.0  # insufficient data

        predictions, outcomes = zip(*pairs)
        return self._spearman_rank_corr(list(predictions), list(outcomes))

    @staticmethod
    def _spearman_rank_corr(x: list[float], y: list[float]) -> float:
        """Spearman rank correlation without scipy."""
        n = len(x)
        if n < 3:
            return 0.0

        def _rank(vals):
            indexed = sorted(enumerate(vals), key=lambda t: t[1])
            ranks = [0.0] * n
            i = 0
            while i < n:
                j = i
                while j < n - 1 and indexed[j + 1][1] == indexed[j][1]:
                    j += 1
                avg_rank = (i + j) / 2.0 + 1.0
                for k in range(i, j + 1):
                    ranks[indexed[k][0]] = avg_rank
                i = j + 1
            return ranks

        rx = _rank(x)
        ry = _rank(y)

        d_sq = sum((a - b) ** 2 for a, b in zip(rx, ry))
        return 1.0 - (6.0 * d_sq) / (n * (n * n - 1))

    def get_brier_decomposition(self, strategy: str = "",
                                 window: int = 100,
                                 n_bins: int = 10) -> dict:
        """
        Brier Score Decomposition (Murphy 1973).

        Brier = Reliability - Resolution + Uncertainty

        - Reliability (lower = better): miscalibration penalty.
          Σ n_k/N * (f_k - o_k)² where f_k = avg predicted prob in bin k,
          o_k = observed frequency in bin k.
        - Resolution (higher = better): how much predictions differ from base rate.
          Σ n_k/N * (o_k - base_rate)²
        - Uncertainty: base_rate * (1 - base_rate) — inherent, irreducible.
        """
        relevant = self._completed
        if strategy:
            relevant = [a for a in relevant if a.strategy == strategy]
        relevant = relevant[-window:]

        pairs = [
            (a.win_prob_predicted, 1.0 if a.won else 0.0)
            for a in relevant
            if a.win_prob_predicted > 0
        ]

        if len(pairs) < 10:
            return {
                "brier": 0.25, "reliability": 0.0,
                "resolution": 0.0, "uncertainty": 0.25,
                "n": len(pairs),
            }

        predictions, outcomes = zip(*pairs)
        n = len(predictions)
        base_rate = sum(outcomes) / n
        uncertainty = base_rate * (1.0 - base_rate)

        # Bin predictions into n_bins equal-width bins [0, 1]
        bins: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for p, o in zip(predictions, outcomes):
            bin_idx = min(int(p * n_bins), n_bins - 1)
            bins[bin_idx].append((p, o))

        reliability = 0.0
        resolution = 0.0
        for bin_idx, bin_data in bins.items():
            n_k = len(bin_data)
            f_k = sum(p for p, _ in bin_data) / n_k  # avg predicted prob
            o_k = sum(o for _, o in bin_data) / n_k   # observed frequency
            reliability += (n_k / n) * (f_k - o_k) ** 2
            resolution += (n_k / n) * (o_k - base_rate) ** 2

        brier = reliability - resolution + uncertainty

        return {
            "brier": round(brier, 4),
            "reliability": round(reliability, 4),
            "resolution": round(resolution, 4),
            "uncertainty": round(uncertainty, 4),
            "base_rate": round(base_rate, 3),
            "n": n,
        }

    def get_ic_decay(self, strategy: str, windows: list[int] | None = None) -> dict:
        """
        IC decay over multiple time windows.
        Detects signal staleness: if IC is declining, model is losing edge.
        """
        if windows is None:
            windows = [20, 50, 100]

        result = {}
        for w in windows:
            ic = self.get_information_coefficient(strategy=strategy, window=w)
            result[f"ic_{w}"] = round(ic, 4)

        # IC trend: compare shortest vs longest window
        if len(windows) >= 2:
            ic_recent = result.get(f"ic_{windows[0]}", 0)
            ic_long = result.get(f"ic_{windows[-1]}", 0)
            result["ic_trend"] = "declining" if ic_recent < ic_long - 0.03 else (
                "improving" if ic_recent > ic_long + 0.03 else "stable"
            )

        return result

    @property
    def report(self) -> dict:
        """Report completo di attribution con IC e Brier decomposition."""
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

        # IC per strategia
        ic_scores = {s: self.get_information_coefficient(strategy=s) for s in strategies}

        # Brier decomposition per strategia
        brier_decomp = {
            s: self.get_brier_decomposition(strategy=s) for s in strategies
        }

        return {
            "pnl_by_signal": dict(pnl_by_signal),
            "pnl_by_category": dict(pnl_by_category),
            "pnl_by_strategy": dict(pnl_by_strategy),
            "count_by_signal": dict(count_by_signal),
            "brier_scores": brier_scores,
            "information_coefficients": {
                s: round(v, 4) for s, v in ic_scores.items()
            },
            "brier_decomposition": brier_decomp,
            "total_tracked": len(self._completed),
            "active_trades": len(self._active),
        }
