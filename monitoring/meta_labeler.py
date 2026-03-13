"""
Meta-Labeling v1.1 (Lopez de Prado, AFML Ch 3)

The primary model (weather filters) decides DIRECTION.
The meta-labeler decides SIZE via P(profitable) multiplier.

Two phases:
  Phase 1 (cold start, <50 trades): Rule-based scoring from empirical patterns
  Phase 2 (warm, >=50 trades): Logistic regression on feature vector

v1.1: TSFresh feature augmentation — 14 base features + N TSFresh features.
  TSFresh features are extracted every 200 trades (cached) and appended
  to the base feature vector. The model adapts to variable-length vectors.

Output: meta_probability in [0, 1] = P(trade profitable | signal generated)
Usage: kelly_size *= meta_probability (continuous sizing, not binary gate)
"""

import json
import logging
import os
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:
    np = None
    logger.warning("[META-LABEL] numpy non disponibile — Phase 2 disabilitata")

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    logger.info("[META-LABEL] sklearn non disponibile — solo Phase 1 rule-based")

# TSFresh feature augmentation (v1.1)
try:
    from utils.feature_engine import extract_trade_features
    HAS_TSFRESH_ENGINE = True
except ImportError:
    HAS_TSFRESH_ENGINE = False
    logger.info("[META-LABEL] feature_engine not available — TSFresh augmentation disabled")


@dataclass
class MetaFeatures:
    n_sources: int          # from forecast.n_sources
    sigma: float            # forecast uncertainty
    spread: float           # market spread (1 - price_yes - price_no)
    volume_24h: float       # market volume
    price: float            # buy price
    days_ahead: int         # time to resolution
    hour_utc: int           # hour of trade (GFS/ECMWF cycles)
    edge: float             # estimated edge
    confidence: float       # composite confidence
    side: int               # 0=BUY_NO, 1=BUY_YES
    expected_value: float   # EV per $1
    payoff_ratio: float     # (1/price) - 1
    is_latency_opp: bool    # from forecast shift detection
    bucket_width: float     # high - low

    def to_vector(self) -> list[float]:
        return [
            self.n_sources,
            self.sigma,
            self.spread,
            min(self.volume_24h / 100_000, 5.0),  # normalize
            self.price,
            self.days_ahead,
            self.hour_utc / 24.0,  # normalize
            self.edge,
            self.confidence,
            self.side,
            self.expected_value,
            self.payoff_ratio,
            1.0 if self.is_latency_opp else 0.0,
            min(self.bucket_width / 10.0, 5.0),  # normalize
        ]


class MetaLabeler:
    """Secondary model: predicts P(trade profitable | signal generated)."""

    MIN_SAMPLES_WARM = 50
    RETRAIN_INTERVAL = 10
    SAVE_PATH = "logs/meta_labeler.json"
    BASE_FEATURE_COUNT = 14  # original MetaFeatures vector length

    def __init__(self):
        self._features: list[list[float]] = []
        self._labels: list[int] = []  # 1=WIN, 0=LOSS
        self._model = None
        self._scaler = None
        self._cold_start = True
        self._trades_since_retrain = 0
        self._total_predictions = 0
        # TSFresh augmentation (v1.1)
        self._tsfresh_features: dict[str, float] = {}
        self._tsfresh_feature_names: list[str] = []  # ordered feature names for consistency
        self._trades_history: list[dict] = []  # raw trades for TSFresh extraction

    def set_trades_history(self, trades: list[dict]):
        """
        Provide raw trade history for TSFresh feature extraction (v1.1).

        Call this periodically (e.g., at bot startup and every 200 trades)
        with the full trades list from trades.json.
        """
        self._trades_history = trades
        self._refresh_tsfresh_features()

    def _refresh_tsfresh_features(self):
        """Re-extract TSFresh features if enough new trades have arrived."""
        if not HAS_TSFRESH_ENGINE or not self._trades_history:
            return

        try:
            features = extract_trade_features(self._trades_history, window=50)
            if features:
                self._tsfresh_features = features
                # Lock feature names on first extraction; keep consistent order
                if not self._tsfresh_feature_names:
                    self._tsfresh_feature_names = sorted(features.keys())
                logger.info(
                    f"[META-LABEL] TSFresh augmentation: {len(self._tsfresh_feature_names)} "
                    f"features available"
                )
        except Exception as e:
            logger.debug(f"[META-LABEL] TSFresh refresh failed: {e}")

    def _augmented_vector(self, base_vector: list[float]) -> list[float]:
        """
        Augment base 14-feature vector with TSFresh features (v1.1).

        Returns the base vector if TSFresh is unavailable or no features extracted.
        The TSFresh features are appended in a fixed order (sorted by name).
        """
        if not self._tsfresh_features or not self._tsfresh_feature_names:
            return base_vector

        tsfresh_values = [
            self._tsfresh_features.get(name, 0.0)
            for name in self._tsfresh_feature_names
        ]
        return base_vector + tsfresh_values

    def predict(self, features: MetaFeatures) -> float:
        """Return P(profitable) in [0, 1]."""
        self._total_predictions += 1
        if self._cold_start or self._model is None:
            return self._rule_based_score(features)
        return self._model_predict(features)

    def record_outcome(self, features: MetaFeatures, won: bool):
        """Record a resolved trade outcome for training."""
        # v1.1: store augmented vector (base 14 + TSFresh features)
        augmented = self._augmented_vector(features.to_vector())
        self._features.append(augmented)
        self._labels.append(1 if won else 0)
        self._trades_since_retrain += 1

        # Refresh TSFresh features periodically
        if HAS_TSFRESH_ENGINE and len(self._labels) % 200 == 0:
            self._refresh_tsfresh_features()

        if len(self._labels) >= self.MIN_SAMPLES_WARM and HAS_SKLEARN and np is not None:
            if self._cold_start or self._trades_since_retrain >= self.RETRAIN_INTERVAL:
                self._retrain()

        # Auto-save periodically
        if len(self._labels) % 5 == 0:
            self.save()

    def _rule_based_score(self, f: MetaFeatures) -> float:
        """Phase 1: Empirical rules from historical patterns."""
        score = 0.70  # base: weather ~74% historical WR

        # Multi-source much better
        if f.n_sources >= 3:
            score += 0.08
        elif f.n_sources >= 2:
            score += 0.04
        else:
            score -= 0.10

        # BUY_YES historically 12% WR
        if f.side == 1:
            score -= 0.30

        # Same-day much more accurate
        if f.days_ahead == 0:
            score += 0.05
        elif f.days_ahead >= 2:
            score -= 0.08

        # High sigma = uncertain forecast
        if f.sigma > 4.0:
            score -= 0.12
        elif f.sigma > 2.5:
            score -= 0.06

        # Low price (high payoff) historically 100% WR
        if f.price < 0.20:
            score += 0.10
        elif f.price > 0.70:
            score -= 0.05

        # High edge = stronger signal
        if f.edge > 0.30:
            score += 0.05
        elif f.edge < 0.08:
            score -= 0.05

        return max(0.10, min(0.95, score))

    def _retrain(self):
        """Phase 2: Fit logistic regression on accumulated data."""
        if not HAS_SKLEARN or np is None:
            return

        # v1.1: handle variable-length feature vectors.
        # Old records may have 14 features, new ones 14+N (TSFresh augmented).
        # Pad shorter vectors with 0.0 to match the longest.
        max_len = max(len(f) for f in self._features)
        padded = [
            f + [0.0] * (max_len - len(f)) for f in self._features
        ]

        X = np.array(padded)
        y = np.array(self._labels)

        if len(np.unique(y)) < 2:
            logger.debug("[META-LABEL] Retrain skipped: single class in labels")
            return

        try:
            self._scaler = StandardScaler()
            X_scaled = self._scaler.fit_transform(X)

            self._model = LogisticRegression(
                C=1.0, class_weight='balanced', max_iter=200, random_state=42
            )
            self._model.fit(X_scaled, y)

            # In-sample accuracy
            acc = self._model.score(X_scaled, y)
            self._cold_start = False
            self._trades_since_retrain = 0

            n_base = self.BASE_FEATURE_COUNT
            n_tsfresh = max_len - n_base
            logger.info(
                f"[META-LABEL] Phase 2 trained: {len(y)} samples, "
                f"acc={acc:.3f}, WR={y.mean():.3f}, "
                f"features={n_base}+{n_tsfresh} (base+tsfresh)"
            )
        except Exception as e:
            logger.warning(f"[META-LABEL] Retrain failed: {e}")

    def _model_predict(self, features: MetaFeatures) -> float:
        """Return calibrated probability from logistic regression."""
        try:
            # v1.1: augment with TSFresh features and pad to model's expected width
            base = features.to_vector()
            augmented = self._augmented_vector(base)
            expected_width = self._model.n_features_in_
            # Pad or truncate to match model width
            if len(augmented) < expected_width:
                augmented = augmented + [0.0] * (expected_width - len(augmented))
            elif len(augmented) > expected_width:
                augmented = augmented[:expected_width]

            X = np.array([augmented])
            X_scaled = self._scaler.transform(X)
            prob = self._model.predict_proba(X_scaled)[0][1]
            return float(prob)
        except Exception:
            return self._rule_based_score(features)

    def save(self, path: str = None):
        """Persist features + labels for restart recovery."""
        path = path or self.SAVE_PATH
        data = {
            "features": self._features,
            "labels": self._labels,
            "cold_start": self._cold_start,
            "total_predictions": self._total_predictions,
            "tsfresh_feature_names": self._tsfresh_feature_names,
        }
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"[META-LABEL] Save failed: {e}")

    def load(self, path: str = None):
        """Restore state across restarts."""
        path = path or self.SAVE_PATH
        try:
            with open(path) as f:
                data = json.load(f)
            self._features = data.get("features", [])
            self._labels = data.get("labels", [])
            self._total_predictions = data.get("total_predictions", 0)
            self._tsfresh_feature_names = data.get("tsfresh_feature_names", [])

            if len(self._labels) >= self.MIN_SAMPLES_WARM and HAS_SKLEARN and np is not None:
                self._retrain()
            else:
                self._cold_start = True

            logger.info(
                f"[META-LABEL] Loaded: {len(self._labels)} samples, "
                f"phase={'2 (warm)' if not self._cold_start else '1 (cold)'}, "
                f"tsfresh_features={len(self._tsfresh_feature_names)}"
            )
        except FileNotFoundError:
            logger.info("[META-LABEL] No saved state, starting fresh (Phase 1)")
        except Exception as e:
            logger.warning(f"[META-LABEL] Load failed: {e}")

    def status(self) -> dict:
        n = len(self._labels)
        wr = sum(self._labels) / n if n > 0 else 0
        return {
            "phase": 2 if not self._cold_start else 1,
            "samples": n,
            "wr": round(wr, 3),
            "predictions": self._total_predictions,
            "warm_at": self.MIN_SAMPLES_WARM,
            "tsfresh_features": len(self._tsfresh_feature_names),
            "tsfresh_available": HAS_TSFRESH_ENGINE,
        }
