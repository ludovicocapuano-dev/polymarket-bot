"""
Signal Validator v9.2.1 — Gate finale prima dell'esecuzione.

8 gate checks:
1. Min edge threshold (>=0.02)
2. Confidence >= 60%
3. Resolution clarity (end_date < 30gg)
4. Liquidità >= 2x trade size
5. Spread <= 5%
6. EV positivo dopo fee round-trip
7. Non flaggato dal Devil's Advocate
8. VPIN < 0.7 (no toxic flow — Easley, López de Prado, O'Hara 2012)
"""

from dataclasses import dataclass, field
from enum import Enum
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class ValidationResult(Enum):
    TRADE = "TRADE"
    SKIP = "SKIP"
    REVIEW = "REVIEW"

@dataclass
class UnifiedSignal:
    """Segnale normalizzato da qualsiasi strategia."""
    strategy: str
    market_id: str
    question: str
    side: str           # "YES" o "NO"
    price: float
    edge: float
    confidence: float
    signal_type: str    # "news_reactive", "structural", "bond", "whale_copy", "weather", "arb", "data_driven"
    category: str = ""
    volume: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0
    news_strength: float = 0.0
    whale_consensus: float = 0.0
    days_to_resolution: float = -1.0  # -1 = sconosciuto
    reasoning: str = ""
    expected_value: float = 0.0
    kelly_size: float = 0.0

@dataclass
class SignalReport:
    result: ValidationResult
    score: float              # 0-1 quality score
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    devil_advocate_flag: bool = False
    devil_advocate_reason: str = ""

class SignalValidator:
    """Valida segnali con 8 gate checks prima dell'esecuzione."""

    # Fee rate Polymarket taker
    FEE_RATE = 0.0625

    # Soglie gate
    MIN_EDGE = 0.02
    MIN_CONFIDENCE = 0.60
    MAX_DAYS_TO_RESOLUTION = 30
    MIN_LIQUIDITY_MULTIPLIER = 2.0
    MAX_SPREAD = 0.05
    VPIN_TOXIC_THRESHOLD = 0.7  # v9.2.1: VPIN > 0.7 = toxic flow

    def __init__(self, devil_advocate=None, vpin_monitor=None):
        self.devil_advocate = devil_advocate
        self.vpin_monitor = vpin_monitor  # v9.2.1

    def validate(self, signal: UnifiedSignal, trade_size: float) -> SignalReport:
        """Esegue 7 gate checks e ritorna un SignalReport."""
        passed = []
        failed = []

        # Gate 1: Min edge
        if signal.edge >= self.MIN_EDGE:
            passed.append(f"edge={signal.edge:.4f} >= {self.MIN_EDGE}")
        else:
            failed.append(f"edge={signal.edge:.4f} < {self.MIN_EDGE}")

        # Gate 2: Confidence
        if signal.confidence >= self.MIN_CONFIDENCE:
            passed.append(f"confidence={signal.confidence:.2f} >= {self.MIN_CONFIDENCE}")
        else:
            failed.append(f"confidence={signal.confidence:.2f} < {self.MIN_CONFIDENCE}")

        # Gate 3: Resolution clarity
        if signal.days_to_resolution < 0:
            passed.append("days_to_resolution=unknown (skip check)")
        elif signal.days_to_resolution <= self.MAX_DAYS_TO_RESOLUTION:
            passed.append(f"days_to_resolution={signal.days_to_resolution:.1f} <= {self.MAX_DAYS_TO_RESOLUTION}")
        else:
            failed.append(f"days_to_resolution={signal.days_to_resolution:.1f} > {self.MAX_DAYS_TO_RESOLUTION}")

        # Gate 4: Liquidità
        if signal.liquidity <= 0:
            passed.append("liquidity=unknown (skip check)")
        elif signal.liquidity >= self.MIN_LIQUIDITY_MULTIPLIER * trade_size:
            passed.append(f"liquidity=${signal.liquidity:.0f} >= {self.MIN_LIQUIDITY_MULTIPLIER}x ${trade_size:.0f}")
        else:
            failed.append(f"liquidity=${signal.liquidity:.0f} < {self.MIN_LIQUIDITY_MULTIPLIER}x ${trade_size:.0f}")

        # Gate 5: Spread
        if signal.spread <= 0:
            passed.append("spread=unknown (skip check)")
        elif signal.spread <= self.MAX_SPREAD:
            passed.append(f"spread={signal.spread:.4f} <= {self.MAX_SPREAD}")
        else:
            failed.append(f"spread={signal.spread:.4f} > {self.MAX_SPREAD}")

        # Gate 6: EV positivo dopo fee round-trip
        price = signal.price
        if 0 < price < 1:
            entry_fee = price * (1.0 - price) * self.FEE_RATE
            exit_fee = price * (1.0 - price) * self.FEE_RATE
            total_fee = entry_fee + exit_fee + 0.005  # + spread
            ev = signal.edge - total_fee
            if ev > 0:
                passed.append(f"EV={ev:.4f} > 0 (fee_rt={total_fee:.4f})")
            else:
                failed.append(f"EV={ev:.4f} <= 0 (fee_rt={total_fee:.4f})")
        else:
            passed.append("EV check skipped (price out of range)")

        # Gate 7: Devil's Advocate
        da_flagged = False
        da_reason = ""
        if self.devil_advocate:
            da_flagged, da_reason = self.devil_advocate.challenge(signal)
            if not da_flagged:
                passed.append("devil_advocate=CLEAR")
            else:
                failed.append(f"devil_advocate=FLAGGED: {da_reason}")
        else:
            passed.append("devil_advocate=disabled")

        # Gate 8: VPIN toxic flow (v9.2.1 Stoikov)
        if self.vpin_monitor and signal.market_id:
            vpin = self.vpin_monitor.get_vpin(signal.market_id)
            if vpin < self.VPIN_TOXIC_THRESHOLD:
                passed.append(f"vpin={vpin:.3f} < {self.VPIN_TOXIC_THRESHOLD}")
            else:
                failed.append(f"vpin={vpin:.3f} >= {self.VPIN_TOXIC_THRESHOLD} (toxic flow)")
        else:
            passed.append("vpin=disabled")

        # Calcola score (0-1)
        total_checks = len(passed) + len(failed)
        score = len(passed) / total_checks if total_checks > 0 else 0.0

        # Determina risultato
        if len(failed) == 0:
            result = ValidationResult.TRADE
        elif len(failed) <= 1 and score >= 0.7:
            result = ValidationResult.REVIEW
        else:
            result = ValidationResult.SKIP

        report = SignalReport(
            result=result,
            score=score,
            checks_passed=passed,
            checks_failed=failed,
            devil_advocate_flag=da_flagged,
            devil_advocate_reason=da_reason,
        )

        logger.info(
            f"[VALIDATOR] {signal.strategy}/{signal.signal_type} "
            f"{signal.side} @{signal.price:.4f} edge={signal.edge:.4f} "
            f"→ {result.value} (score={score:.2f}, "
            f"passed={len(passed)}, failed={len(failed)})"
        )
        if failed:
            logger.debug(f"[VALIDATOR] Failed: {', '.join(failed)}")

        return report
