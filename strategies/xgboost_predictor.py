"""
XGBoost Prediction Strategy v1.0 — Sequential Correction Trees
================================================================

Based on the approach of a trader with 89.6% accuracy, 1870 trades, +105% backtest.

Model: XGBClassifier with 330 sequential correction trees (boosted stumps, max_depth=1).

Features (7):
1. contract_price — current YES price from Polymarket
2. volume_24h — 24h trading volume (log-scaled)
3. momentum_7d — price change over 7 days
4. days_to_expiry — days until market resolves
5. liquidity — market depth/liquidity (log-scaled)
6. rsi_14 — 14-period RSI (overbought/oversold)
7. macd — MACD(12,26,9) histogram (trend direction)

Entry rule: ONLY buy when contract_price <= model_probability * 0.5
  (Massive margin of safety: "Enter when the market is wrong twice over")

Exit rule: Sell when contract_price >= model_probability * 0.9 OR days_to_expiry <= 7

Sizing: Kelly f* = (p - m) / (1 - m) * 0.25, capped at $60.
"""

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import xgboost as xgb
    HAS_XGB = True
except ImportError:
    HAS_XGB = False

from utils.market_db import db as market_db
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────

STRATEGY_NAME = "xgboost_pred"
GAMMA_API = "https://gamma-api.polymarket.com/markets"
MODEL_PATH = Path("logs/xgboost_model.json")
TRAINING_DATA_PATH = Path("logs/xgboost_training_data.json")
FEATURE_IMPORTANCE_PATH = Path("logs/xgboost_feature_importance.json")
PREDICTIONS_LOG_PATH = Path("logs/xgboost_predictions.json")

# Strategy parameters
MAX_BET = 60.0
MIN_BET = 5.0
KELLY_FRACTION = 0.25
MAX_OPEN_POSITIONS = 15
MIN_TRAINING_SAMPLES = 100
RETRAIN_INTERVAL_DAYS = 7
MIN_VOLUME = 5_000         # $5K minimum volume
MIN_LIQUIDITY = 1_000      # $1K minimum liquidity
ENTRY_MULTIPLIER = 0.5     # contract_price <= model_prob * 0.5
EXIT_MULTIPLIER = 0.9      # sell when price >= model_prob * 0.9
EXIT_DAYS_THRESHOLD = 7    # sell if <= 7 days to expiry
MAX_HOLD_DAYS = 14         # absolute max hold

# Feature names in order
FEATURE_NAMES = [
    "contract_price", "volume_24h", "momentum_7d",
    "days_to_expiry", "liquidity", "rsi_14", "macd",
]

# Median defaults for missing features (conservative)
FEATURE_MEDIANS = {
    "contract_price": 0.50,
    "volume_24h": 10.0,     # log scale
    "momentum_7d": 0.0,
    "days_to_expiry": 30.0,
    "liquidity": 8.0,       # log scale
    "rsi_14": 50.0,
    "macd": 0.0,
}


# ══════════════════════════════════════════════════════════════
#  FeatureExtractor — computes all 7 features for a market
# ══════════════════════════════════════════════════════════════

class FeatureExtractor:
    """
    Extract 7 features from a market object + price history from DB.

    Handles missing data gracefully — fills with median values.
    """

    def __init__(self, db=None):
        self.db = db or market_db

    def extract(self, market, price_history: list[dict] = None) -> np.ndarray:
        """
        Extract features for a single market.

        Args:
            market: Market object from PolymarketAPI (has .prices, .volume, .liquidity, .end_date, .id)
            price_history: optional pre-fetched price history (list of dicts with 'price', 'snapshot_time')

        Returns:
            numpy array of 7 features
        """
        features = {}

        # 1. contract_price — current YES price
        try:
            if hasattr(market, 'prices'):
                features["contract_price"] = float(market.prices.get("yes", 0.5))
            elif isinstance(market, dict):
                features["contract_price"] = float(market.get("yes_price", 0.5))
            else:
                features["contract_price"] = FEATURE_MEDIANS["contract_price"]
        except (TypeError, ValueError):
            features["contract_price"] = FEATURE_MEDIANS["contract_price"]

        # 2. volume_24h — log-scaled 24h volume
        try:
            vol = 0.0
            if hasattr(market, 'volume'):
                vol = float(market.volume or 0)
            elif isinstance(market, dict):
                vol = float(market.get("volume", 0))
            features["volume_24h"] = math.log1p(vol) if vol > 0 else FEATURE_MEDIANS["volume_24h"]
        except (TypeError, ValueError):
            features["volume_24h"] = FEATURE_MEDIANS["volume_24h"]

        # 3. momentum_7d — price change over 7 days from DB snapshots
        features["momentum_7d"] = self._calc_momentum_7d(market, price_history)

        # 4. days_to_expiry — parse endDate
        features["days_to_expiry"] = self._calc_days_to_expiry(market)

        # 5. liquidity — log-scaled
        try:
            liq = 0.0
            if hasattr(market, 'liquidity'):
                liq = float(market.liquidity or 0)
            elif isinstance(market, dict):
                liq = float(market.get("liquidity", 0))
            features["liquidity"] = math.log1p(liq) if liq > 0 else FEATURE_MEDIANS["liquidity"]
        except (TypeError, ValueError):
            features["liquidity"] = FEATURE_MEDIANS["liquidity"]

        # 6. rsi_14 — from price history
        features["rsi_14"] = self._calc_rsi(price_history)

        # 7. macd — MACD(12,26,9) histogram from price history
        features["macd"] = self._calc_macd(price_history)

        # Build ordered array
        result = np.array(
            [features.get(name, FEATURE_MEDIANS[name]) for name in FEATURE_NAMES],
            dtype=np.float32,
        )

        # Replace NaN/inf with medians
        for i, name in enumerate(FEATURE_NAMES):
            if not np.isfinite(result[i]):
                result[i] = FEATURE_MEDIANS[name]

        return result

    def _calc_momentum_7d(self, market, price_history: list[dict] = None) -> float:
        """Price change over 7 days. Returns 0 if insufficient data."""
        try:
            market_id = market.id if hasattr(market, 'id') else market.get("market_id", "")
            if not market_id:
                return FEATURE_MEDIANS["momentum_7d"]

            # Use provided price history or fetch from DB
            if price_history is None:
                price_history = self.db.get_price_history(market_id, hours=7 * 24 + 1)

            if not price_history or len(price_history) < 2:
                return FEATURE_MEDIANS["momentum_7d"]

            # Filter to YES outcome only
            yes_prices = [
                h for h in price_history
                if h.get("outcome", "").lower() in ("yes", "")
            ]
            if len(yes_prices) < 2:
                return FEATURE_MEDIANS["momentum_7d"]

            current_price = yes_prices[-1]["price"]
            # Find price closest to 7 days ago
            now = datetime.now(timezone.utc)
            target_time = now - timedelta(days=7)
            target_ts = target_time.isoformat()

            oldest_price = yes_prices[0]["price"]
            for h in yes_prices:
                if h.get("snapshot_time", "") <= target_ts:
                    oldest_price = h["price"]
                else:
                    break

            if oldest_price > 0:
                return (current_price - oldest_price) / oldest_price
            return FEATURE_MEDIANS["momentum_7d"]

        except Exception as e:
            logger.debug(f"[XGBOOST] Momentum calc error: {e}")
            return FEATURE_MEDIANS["momentum_7d"]

    def _calc_days_to_expiry(self, market) -> float:
        """Days until market resolves."""
        try:
            end_date = ""
            if hasattr(market, 'end_date'):
                end_date = market.end_date
            elif isinstance(market, dict):
                end_date = market.get("end_date", "")

            if not end_date:
                return FEATURE_MEDIANS["days_to_expiry"]

            # Parse ISO date
            if isinstance(end_date, str):
                # Handle multiple formats
                for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(end_date[:26], fmt)
                        dt = dt.replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                else:
                    return FEATURE_MEDIANS["days_to_expiry"]
            else:
                return FEATURE_MEDIANS["days_to_expiry"]

            days = (dt - datetime.now(timezone.utc)).total_seconds() / 86400
            return max(days, 0.0)

        except Exception as e:
            logger.debug(f"[XGBOOST] Days to expiry calc error: {e}")
            return FEATURE_MEDIANS["days_to_expiry"]

    def _calc_rsi(self, price_history: list[dict] = None, period: int = 14) -> float:
        """RSI(14) from daily closing prices."""
        try:
            if not price_history or len(price_history) < period + 1:
                return FEATURE_MEDIANS["rsi_14"]

            # Get daily closes (YES outcome)
            yes_prices = [
                h for h in price_history
                if h.get("outcome", "").lower() in ("yes", "")
            ]
            if len(yes_prices) < period + 1:
                return FEATURE_MEDIANS["rsi_14"]

            # Sample daily (take every ~24h worth of data points)
            # Use last N+1 prices for N changes
            closes = [h["price"] for h in yes_prices]
            if len(closes) > 100:
                # Subsample to ~daily resolution
                step = max(1, len(closes) // 30)
                closes = closes[::step]

            if len(closes) < period + 1:
                return FEATURE_MEDIANS["rsi_14"]

            # Use last period+1 closes
            closes = closes[-(period + 1):]
            changes = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]

            gains = [max(c, 0) for c in changes]
            losses = [max(-c, 0) for c in changes]

            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period

            if avg_loss == 0:
                return 100.0 if avg_gain > 0 else 50.0

            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
            return rsi

        except Exception as e:
            logger.debug(f"[XGBOOST] RSI calc error: {e}")
            return FEATURE_MEDIANS["rsi_14"]

    def _calc_macd(self, price_history: list[dict] = None) -> float:
        """MACD(12,26,9) histogram from price history."""
        try:
            if not price_history or len(price_history) < 30:
                return FEATURE_MEDIANS["macd"]

            yes_prices = [
                h for h in price_history
                if h.get("outcome", "").lower() in ("yes", "")
            ]
            if len(yes_prices) < 30:
                return FEATURE_MEDIANS["macd"]

            closes = [h["price"] for h in yes_prices]
            if len(closes) > 200:
                step = max(1, len(closes) // 60)
                closes = closes[::step]

            if len(closes) < 30:
                return FEATURE_MEDIANS["macd"]

            # EMA helper
            def ema(data, period):
                if len(data) < period:
                    return data[-1] if data else 0
                k = 2.0 / (period + 1)
                result = sum(data[:period]) / period
                for price in data[period:]:
                    result = price * k + result * (1 - k)
                return result

            ema12 = ema(closes, 12)
            ema26 = ema(closes, 26)
            macd_line = ema12 - ema26

            # Signal line needs MACD history
            # Compute MACD for last 9+ points
            macd_values = []
            for i in range(26, len(closes)):
                e12 = ema(closes[:i + 1], 12)
                e26 = ema(closes[:i + 1], 26)
                macd_values.append(e12 - e26)

            if len(macd_values) >= 9:
                signal_line = ema(macd_values, 9)
                histogram = macd_values[-1] - signal_line
            else:
                histogram = macd_line

            return histogram

        except Exception as e:
            logger.debug(f"[XGBOOST] MACD calc error: {e}")
            return FEATURE_MEDIANS["macd"]


# ══════════════════════════════════════════════════════════════
#  XGBoostPredictor — trains and predicts with XGBClassifier
# ══════════════════════════════════════════════════════════════

class XGBoostPredictor:
    """
    XGBClassifier with 330 sequential correction trees (boosted stumps).

    Loads trained model from disk if exists, otherwise None (graceful degradation).
    Retrains weekly from resolved markets.
    """

    def __init__(self, model_path: str = None):
        self.model_path = Path(model_path) if model_path else MODEL_PATH
        self.model = None
        self.feature_importance = {}
        self.train_accuracy = 0.0
        self.test_accuracy = 0.0
        self.n_training_samples = 0
        self.last_train_time = 0.0
        self._load_model()

    def _load_model(self):
        """Load trained model from disk if exists."""
        if not HAS_XGB:
            logger.warning("[XGBOOST] xgboost not installed — predictor disabled")
            return

        if self.model_path.exists():
            try:
                self.model = xgb.XGBClassifier()
                self.model.load_model(str(self.model_path))
                # Load metadata
                meta_path = self.model_path.with_suffix(".meta.json")
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text())
                    self.train_accuracy = meta.get("train_accuracy", 0)
                    self.test_accuracy = meta.get("test_accuracy", 0)
                    self.n_training_samples = meta.get("n_training_samples", 0)
                    self.last_train_time = meta.get("last_train_time", 0)
                    self.feature_importance = meta.get("feature_importance", {})
                logger.info(
                    f"[XGBOOST] Model loaded: {self.n_training_samples} samples, "
                    f"train_acc={self.train_accuracy:.1%}, test_acc={self.test_accuracy:.1%}, "
                    f"age={(time.time() - self.last_train_time) / 86400:.1f}d"
                )
            except Exception as e:
                logger.warning(f"[XGBOOST] Failed to load model: {e}")
                self.model = None

    @property
    def is_trained(self) -> bool:
        return self.model is not None

    @property
    def needs_retrain(self) -> bool:
        """Check if model needs retraining (weekly)."""
        if not self.is_trained:
            return True
        age_days = (time.time() - self.last_train_time) / 86400
        return age_days >= RETRAIN_INTERVAL_DAYS

    def train(self, features_array: np.ndarray, labels: np.ndarray) -> bool:
        """
        Train XGBClassifier on historical resolved markets.

        Args:
            features_array: (N, 7) numpy array of features
            labels: (N,) numpy array of labels (1=YES, 0=NO)

        Returns:
            True if training successful
        """
        if not HAS_XGB:
            logger.warning("[XGBOOST] xgboost not installed — cannot train")
            return False

        n_samples = len(labels)
        if n_samples < MIN_TRAINING_SAMPLES:
            logger.info(
                f"[XGBOOST] Insufficient training data: {n_samples} < {MIN_TRAINING_SAMPLES}"
            )
            return False

        logger.info(f"[XGBOOST] Training on {n_samples} samples...")

        # Temporal split: 70% train, 30% test (last 30% is test)
        split_idx = int(n_samples * 0.7)
        X_train, X_test = features_array[:split_idx], features_array[split_idx:]
        y_train, y_test = labels[:split_idx], labels[split_idx:]

        # Check class balance
        train_pos = y_train.sum()
        train_neg = len(y_train) - train_pos
        if train_pos < 5 or train_neg < 5:
            logger.warning(
                f"[XGBOOST] Severely imbalanced: {int(train_pos)} YES, {int(train_neg)} NO — skip"
            )
            return False

        # Scale positive weight for imbalance
        scale_pos = train_neg / max(train_pos, 1)

        try:
            self.model = xgb.XGBClassifier(
                objective="binary:logistic",
                learning_rate=0.1,
                max_depth=1,              # Boosted stumps — low variance
                n_estimators=330,         # 330 sequential correction trees
                scale_pos_weight=scale_pos,
                eval_metric="logloss",
                use_label_encoder=False,
                verbosity=0,
                random_state=42,
            )
            self.model.fit(
                X_train, y_train,
                eval_set=[(X_test, y_test)],
                verbose=False,
            )

            # Evaluate
            train_pred = self.model.predict(X_train)
            test_pred = self.model.predict(X_test)
            self.train_accuracy = (train_pred == y_train).mean()
            self.test_accuracy = (test_pred == y_test).mean()
            self.n_training_samples = n_samples
            self.last_train_time = time.time()

            # Feature importance
            importances = self.model.feature_importances_
            self.feature_importance = {
                name: float(imp)
                for name, imp in zip(FEATURE_NAMES, importances)
            }

            # Save model
            self.model.save_model(str(self.model_path))

            # Save metadata
            meta = {
                "train_accuracy": self.train_accuracy,
                "test_accuracy": self.test_accuracy,
                "n_training_samples": self.n_training_samples,
                "last_train_time": self.last_train_time,
                "feature_importance": self.feature_importance,
                "train_pos": int(train_pos),
                "train_neg": int(train_neg),
                "trained_at": datetime.now(timezone.utc).isoformat(),
            }
            meta_path = self.model_path.with_suffix(".meta.json")
            meta_path.write_text(json.dumps(meta, indent=2))

            # Save feature importance separately for analysis
            FEATURE_IMPORTANCE_PATH.write_text(json.dumps(self.feature_importance, indent=2))

            logger.info(
                f"[XGBOOST] Training complete: "
                f"train_acc={self.train_accuracy:.1%}, test_acc={self.test_accuracy:.1%}, "
                f"n={n_samples} (YES={int(train_pos + y_test.sum())}, "
                f"NO={int(train_neg + len(y_test) - y_test.sum())})"
            )
            logger.info(
                f"[XGBOOST] Feature importance: "
                + ", ".join(f"{k}={v:.3f}" for k, v in
                           sorted(self.feature_importance.items(), key=lambda x: -x[1]))
            )
            return True

        except Exception as e:
            logger.error(f"[XGBOOST] Training failed: {e}", exc_info=True)
            self.model = None
            return False

    def predict(self, features: np.ndarray) -> float:
        """
        Predict probability that market resolves YES.

        Args:
            features: (7,) or (N, 7) numpy array

        Returns:
            probability 0.0 to 1.0 (or 0.5 if model not trained)
        """
        if not self.is_trained:
            return 0.5  # No model — return uninformative prior

        try:
            if features.ndim == 1:
                features = features.reshape(1, -1)
            proba = self.model.predict_proba(features)
            # proba is (N, 2) — column 1 is P(YES)
            return float(proba[0, 1])
        except Exception as e:
            logger.debug(f"[XGBOOST] Prediction error: {e}")
            return 0.5

    def predict_batch(self, features_array: np.ndarray) -> np.ndarray:
        """Predict for multiple markets at once."""
        if not self.is_trained:
            return np.full(len(features_array), 0.5)

        try:
            proba = self.model.predict_proba(features_array)
            return proba[:, 1].astype(float)
        except Exception as e:
            logger.debug(f"[XGBOOST] Batch prediction error: {e}")
            return np.full(len(features_array), 0.5)


# ══════════════════════════════════════════════════════════════
#  Training Data Collector — builds dataset from resolved markets
# ══════════════════════════════════════════════════════════════

@dataclass
class TrainingDataPoint:
    """One resolved market with features and outcome."""
    market_id: str
    features: list[float]  # 7 features
    label: int             # 1=YES, 0=NO
    end_date: str
    question: str = ""


def collect_training_data(db=None, feature_extractor: FeatureExtractor = None) -> tuple[np.ndarray, np.ndarray]:
    """
    Collect training data from resolved markets in our DB.

    Returns:
        (features_array, labels) or (empty, empty) if insufficient data.
        Arrays are temporally sorted by end_date.
    """
    db = db or market_db
    fe = feature_extractor or FeatureExtractor(db=db)

    data_points: list[TrainingDataPoint] = []

    try:
        # Get resolved markets from DB
        with db.connection() as conn:
            rows = conn.execute("""
                SELECT m.market_id, m.question, m.volume, m.liquidity,
                       m.end_date, m.winner
                FROM markets m
                WHERE m.resolved = 1 AND m.winner IS NOT NULL AND m.winner != ''
                ORDER BY m.end_date ASC
            """).fetchall()

        logger.info(f"[XGBOOST] Found {len(rows)} resolved markets in DB")

        for row in rows:
            market_id = row["market_id"]
            winner = row["winner"].lower().strip()

            # Determine label
            if winner in ("yes", "1", "true"):
                label = 1
            elif winner in ("no", "0", "false"):
                label = 0
            else:
                continue  # Unknown resolution — skip

            # Get price history for this market (up to 30 days before resolution)
            price_history = db.get_price_history(market_id, hours=30 * 24)

            # Build a pseudo-market object for feature extraction
            class _MiniMarket:
                pass

            m = _MiniMarket()
            m.id = market_id
            m.prices = {"yes": 0.5, "no": 0.5}
            m.volume = float(row["volume"] or 0)
            m.liquidity = float(row["liquidity"] or 0)
            m.end_date = row["end_date"] or ""

            # If we have price history, use the price from ~entry time
            # (simulate what we'd have seen at entry — use first available price)
            if price_history:
                yes_hist = [h for h in price_history if h.get("outcome", "").lower() in ("yes", "")]
                if yes_hist:
                    # Use price from the first third of history (entry-like timing)
                    entry_idx = min(len(yes_hist) // 3, len(yes_hist) - 1)
                    m.prices["yes"] = yes_hist[entry_idx]["price"]
                    m.prices["no"] = 1.0 - m.prices["yes"]

            features = fe.extract(m, price_history)
            data_points.append(TrainingDataPoint(
                market_id=market_id,
                features=features.tolist(),
                label=label,
                end_date=m.end_date,
                question=row["question"] or "",
            ))

    except Exception as e:
        logger.error(f"[XGBOOST] Training data collection error: {e}", exc_info=True)

    # Also try to load previously saved training data and merge
    saved_points = _load_saved_training_data()
    if saved_points:
        seen_ids = {dp.market_id for dp in data_points}
        for sp in saved_points:
            if sp.market_id not in seen_ids:
                data_points.append(sp)
                seen_ids.add(sp.market_id)

    if len(data_points) < MIN_TRAINING_SAMPLES:
        logger.info(
            f"[XGBOOST] Only {len(data_points)} training samples "
            f"(need {MIN_TRAINING_SAMPLES}) — collecting more from Gamma API"
        )
        # Try fetching more resolved markets from Gamma API
        extra = _fetch_resolved_from_gamma(fe, existing_ids={dp.market_id for dp in data_points})
        data_points.extend(extra)

    # Sort by end_date (temporal order for proper train/test split)
    data_points.sort(key=lambda dp: dp.end_date)

    # Save for future use
    _save_training_data(data_points)

    if len(data_points) < MIN_TRAINING_SAMPLES:
        logger.info(
            f"[XGBOOST] Still only {len(data_points)} samples — "
            f"need {MIN_TRAINING_SAMPLES}. Collecting data passively."
        )
        return np.array([]), np.array([])

    features_array = np.array([dp.features for dp in data_points], dtype=np.float32)
    labels = np.array([dp.label for dp in data_points], dtype=np.float32)

    logger.info(
        f"[XGBOOST] Training dataset: {len(data_points)} markets, "
        f"{int(labels.sum())} YES ({labels.mean():.1%}), "
        f"{int(len(labels) - labels.sum())} NO"
    )
    return features_array, labels


def _fetch_resolved_from_gamma(fe: FeatureExtractor, existing_ids: set,
                                max_fetch: int = 500) -> list[TrainingDataPoint]:
    """Fetch resolved markets from Gamma API for training data."""
    import requests

    points = []
    try:
        for offset in range(0, max_fetch, 100):
            resp = requests.get(
                GAMMA_API,
                params={
                    "closed": "true",
                    "limit": 100,
                    "offset": offset,
                    "order": "volume",
                    "ascending": "false",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                break

            markets_raw = resp.json()
            if not markets_raw:
                break

            for m_raw in markets_raw:
                market_id = m_raw.get("id", "")
                if market_id in existing_ids:
                    continue

                # Check if resolved with a winner
                if not m_raw.get("resolved"):
                    continue

                # Determine winner from outcomePrices (resolved market has 1.0/0.0)
                outcome_prices = m_raw.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except Exception:
                        continue

                if not outcome_prices or len(outcome_prices) < 2:
                    continue

                try:
                    p_yes = float(outcome_prices[0])
                    p_no = float(outcome_prices[1])
                except (ValueError, TypeError, IndexError):
                    continue

                # For resolved markets, prices are 1.0/0.0
                if p_yes >= 0.95:
                    label = 1
                elif p_no >= 0.95:
                    label = 0
                else:
                    continue  # Not clearly resolved

                # Build mini market
                class _MiniMarket:
                    pass

                mm = _MiniMarket()
                mm.id = market_id
                mm.volume = float(m_raw.get("volume", 0) or 0)
                mm.liquidity = float(m_raw.get("liquidity", 0) or 0)
                mm.end_date = m_raw.get("endDate", "")

                # Use a mid-range price as simulated entry price
                # (We don't have historical data for API-fetched markets)
                # Use volume-weighted estimate: high-volume markets tend to be
                # priced efficiently, so use 0.5 as default
                mm.prices = {"yes": 0.5, "no": 0.5}

                features = fe.extract(mm, price_history=None)
                points.append(TrainingDataPoint(
                    market_id=market_id,
                    features=features.tolist(),
                    label=label,
                    end_date=mm.end_date,
                    question=m_raw.get("question", ""),
                ))
                existing_ids.add(market_id)

            # Rate limit
            time.sleep(0.5)

    except Exception as e:
        logger.warning(f"[XGBOOST] Gamma API fetch error: {e}")

    logger.info(f"[XGBOOST] Fetched {len(points)} resolved markets from Gamma API")
    return points


def _save_training_data(data_points: list[TrainingDataPoint]):
    """Save training data to disk for persistence."""
    try:
        data = [asdict(dp) for dp in data_points[-2000:]]  # Keep last 2000
        TRAINING_DATA_PATH.write_text(json.dumps(data))
    except Exception as e:
        logger.debug(f"[XGBOOST] Save training data error: {e}")


def _load_saved_training_data() -> list[TrainingDataPoint]:
    """Load previously saved training data."""
    if not TRAINING_DATA_PATH.exists():
        return []
    try:
        raw = json.loads(TRAINING_DATA_PATH.read_text())
        return [
            TrainingDataPoint(
                market_id=d["market_id"],
                features=d["features"],
                label=d["label"],
                end_date=d["end_date"],
                question=d.get("question", ""),
            )
            for d in raw
        ]
    except Exception as e:
        logger.debug(f"[XGBOOST] Load training data error: {e}")
        return []


# ══════════════════════════════════════════════════════════════
#  XGBoostSignal — opportunity dataclass
# ══════════════════════════════════════════════════════════════

@dataclass
class XGBoostSignal:
    """An XGBoost-identified trading opportunity."""
    market: object           # Market object from API
    market_id: str
    token_id: str            # YES token ID
    question: str
    side: str                # BUY_YES or BUY_NO
    contract_price: float    # current YES price
    model_prob: float        # model's predicted P(YES)
    edge: float              # model_prob - contract_price (for YES) or inverse
    kelly_size: float        # position size in $
    features: list[float]    # raw features for audit
    confidence: float = 0.0  # model confidence


# ══════════════════════════════════════════════════════════════
#  XGBoostStrategy — scan + execute integrated with bot
# ══════════════════════════════════════════════════════════════

class XGBoostStrategy:
    """
    XGBoost-based prediction strategy for Polymarket.

    Scans active markets, extracts features, runs model predictions,
    and identifies markets where price <= model_probability * 0.5.

    Graceful degradation: if model is not trained, logs features for future training
    and does NOT trade. Once >= 100 resolved markets are available, trains automatically.
    """

    def __init__(self, api=None, risk: RiskManager = None,
                 max_bet: float = MAX_BET, min_bet: float = MIN_BET,
                 kelly_fraction: float = KELLY_FRACTION,
                 max_open_positions: int = MAX_OPEN_POSITIONS):
        self.api = api
        self.risk = risk
        self.max_bet = max_bet
        self.min_bet = min_bet
        self.kelly_fraction = kelly_fraction
        self.max_open_positions = max_open_positions

        self.feature_extractor = FeatureExtractor()
        self.predictor = XGBoostPredictor()

        self._open_positions: dict[str, dict] = {}  # market_id -> {model_prob, entry_price, entry_time}
        self._total_scans = 0
        self._total_signals = 0
        self._total_trades = 0
        self._last_scan = 0.0
        self._predictions_log: list[dict] = []

        # Load open positions from disk
        self._load_positions()

        # Log status
        if self.predictor.is_trained:
            logger.info(
                f"[XGBOOST] Strategy ready: model trained on {self.predictor.n_training_samples} samples, "
                f"test_acc={self.predictor.test_accuracy:.1%}"
            )
        else:
            logger.info("[XGBOOST] Strategy initialized (no trained model yet — collecting data)")

    def scan(self, shared_markets: list = None) -> list[XGBoostSignal]:
        """
        Scan active markets for XGBoost opportunities.

        1. Extract features for each market
        2. Run model.predict()
        3. Apply entry rule: contract_price <= model_probability * 0.5
        4. Apply Kelly sizing
        5. Return opportunities

        Args:
            shared_markets: list of Market objects from bot's main loop

        Returns:
            list of XGBoostSignal opportunities
        """
        self._total_scans += 1

        # Auto-retrain if needed
        if self.predictor.needs_retrain:
            self._try_retrain()

        if not shared_markets:
            return []

        # Filter markets
        candidates = []
        for m in shared_markets:
            try:
                yes_price = m.prices.get("yes", 0.5)
                volume = float(m.volume or 0)
                liquidity = float(m.liquidity or 0)

                # Basic filters
                if volume < MIN_VOLUME:
                    continue
                if liquidity < MIN_LIQUIDITY:
                    continue
                if not m.active:
                    continue
                # Skip extreme prices — no room for the 2x entry rule
                if yes_price <= 0.02 or yes_price >= 0.98:
                    continue
                # Skip markets we already have a position in
                if m.id in self._open_positions:
                    continue
                # Check if we already have too many open positions
                xgb_open = sum(
                    1 for t in self.risk.open_trades if t.strategy == STRATEGY_NAME
                ) if self.risk else 0
                if xgb_open >= self.max_open_positions:
                    break

                candidates.append(m)

            except Exception:
                continue

        if not candidates:
            return []

        # Extract features for all candidates
        features_list = []
        valid_markets = []
        for m in candidates:
            try:
                price_history = market_db.get_price_history(m.id, hours=30 * 24)
                features = self.feature_extractor.extract(m, price_history)
                features_list.append(features)
                valid_markets.append(m)
            except Exception as e:
                logger.debug(f"[XGBOOST] Feature extraction failed for {m.id[:16]}: {e}")
                continue

        if not features_list:
            return []

        # If model not trained, just log features (passive collection)
        if not self.predictor.is_trained:
            logger.info(
                f"[XGBOOST] No model yet — extracted features for {len(valid_markets)} markets "
                f"(collecting data, need {MIN_TRAINING_SAMPLES} resolved to train)"
            )
            return []

        # Batch predict
        features_array = np.array(features_list, dtype=np.float32)
        probabilities = self.predictor.predict_batch(features_array)

        # Apply entry rule and build signals
        signals = []
        for i, (m, prob) in enumerate(zip(valid_markets, probabilities)):
            yes_price = m.prices.get("yes", 0.5)

            # Entry rule: contract_price <= model_probability * ENTRY_MULTIPLIER
            # "Enter when the market is wrong twice over"
            entry_threshold = prob * ENTRY_MULTIPLIER

            # For YES side: price is cheap relative to model
            if yes_price <= entry_threshold and prob > 0.5:
                edge = prob - yes_price
                side = "BUY_YES"
                token_id = m.tokens.get("yes", "")
                contract_price = yes_price

            # For NO side: 1-price is cheap relative to 1-model_prob
            elif (1.0 - yes_price) <= (1.0 - prob) * ENTRY_MULTIPLIER and prob < 0.5:
                edge = (1.0 - yes_price) - (1.0 - prob)  # NO edge
                # Remap: our model says P(YES)=prob, so P(NO)=1-prob
                # The NO price is 1-yes_price. Edge on NO side.
                edge = (1.0 - prob) - (1.0 - yes_price)
                side = "BUY_NO"
                token_id = m.tokens.get("no", "")
                contract_price = 1.0 - yes_price
            else:
                # Log prediction for monitoring
                self._log_prediction(m, prob, yes_price, "SKIP")
                continue

            if edge < 0.03:
                continue  # Minimum 3% edge

            # Kelly sizing: f* = (p - m) / (1 - m) * kelly_fraction
            # p = model probability, m = market price
            if side == "BUY_YES":
                p_model = prob
                p_market = yes_price
            else:
                p_model = 1.0 - prob
                p_market = 1.0 - yes_price

            kelly_raw = (p_model - p_market) / (1.0 - p_market) if p_market < 1.0 else 0.0
            kelly_sized = kelly_raw * self.kelly_fraction

            # Apply risk-manager Kelly if available (includes GARCH, empirical, etc.)
            if self.risk:
                rm_size = self.risk.kelly_size(p_model, p_market, STRATEGY_NAME)
                if rm_size > 0:
                    kelly_sized = min(kelly_sized * self.risk.capital, rm_size)
                else:
                    kelly_sized = kelly_sized * (self.risk.capital if self.risk else 300.0)
            else:
                kelly_sized = kelly_sized * 300.0  # fallback budget

            # Clamp to [min_bet, max_bet]
            size = max(self.min_bet, min(self.max_bet, kelly_sized))

            if size < self.min_bet:
                continue

            # Confidence = how far below threshold (stronger signal = more confident)
            confidence = min(1.0, (entry_threshold - contract_price) / max(entry_threshold, 0.01))

            signal = XGBoostSignal(
                market=m,
                market_id=m.id,
                token_id=token_id,
                question=m.question[:80] if hasattr(m, 'question') else "",
                side=side,
                contract_price=contract_price,
                model_prob=prob,
                edge=edge,
                kelly_size=size,
                features=features_list[i].tolist(),
                confidence=confidence,
            )
            signals.append(signal)
            self._log_prediction(m, prob, yes_price, side)

        # Sort by edge descending
        signals.sort(key=lambda s: s.edge, reverse=True)
        self._total_signals += len(signals)

        if signals:
            logger.info(
                f"[XGBOOST] Scan #{self._total_scans}: {len(candidates)} candidates → "
                f"{len(signals)} signals (top edge={signals[0].edge:.1%}, "
                f"model_prob={signals[0].model_prob:.3f} vs price={signals[0].contract_price:.3f})"
            )
        else:
            logger.debug(
                f"[XGBOOST] Scan #{self._total_scans}: {len(candidates)} candidates → 0 signals"
            )

        return signals

    def execute(self, signal: XGBoostSignal, api=None, risk=None,
                live: bool = False) -> bool:
        """
        Execute an XGBoost trade.

        Args:
            signal: XGBoostSignal with edge meeting entry rule
            api: PolymarketAPI instance
            risk: RiskManager instance
            live: True for real trading, False for paper
        """
        api = api or self.api
        risk = risk or self.risk

        if not api or not risk:
            logger.error("[XGBOOST] No API or risk manager")
            return False

        # Pre-trade checks
        ok, reason = risk.can_trade(
            STRATEGY_NAME, signal.kelly_size,
            price=signal.contract_price, side=signal.side,
            market_id=signal.market_id,
        )
        if not ok:
            logger.info(f"[XGBOOST] Trade blocked: {reason}")
            return False

        logger.info(
            f"[XGBOOST] {'LIVE' if live else 'PAPER'} {signal.side} ${signal.kelly_size:.0f} "
            f"@ {signal.contract_price:.3f} | model_prob={signal.model_prob:.3f} "
            f"edge={signal.edge:.1%} | {signal.question}"
        )

        result = None
        if live:
            try:
                result = api.smart_buy(
                    token_id=signal.token_id,
                    amount=signal.kelly_size,
                    target_price=signal.contract_price,
                )
            except Exception as e:
                logger.error(f"[XGBOOST] Order failed: {e}")
                return False
        else:
            # Paper trade simulation
            import random
            if random.random() < 0.85:  # 85% fill rate
                result = {"status": "MATCHED", "price": signal.contract_price}

        if result:
            fill_price = float(result.get("price", signal.contract_price))

            # Register trade with risk manager
            trade = Trade(
                timestamp=time.time(),
                strategy=STRATEGY_NAME,
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side,
                size=signal.kelly_size,
                price=fill_price,
                edge=signal.edge,
                reason=f"XGB prob={signal.model_prob:.3f} edge={signal.edge:.1%}",
                confidence=signal.confidence,
            )
            risk.register_trade(trade)

            # Track for exit management
            self._open_positions[signal.market_id] = {
                "model_prob": signal.model_prob,
                "entry_price": fill_price,
                "entry_time": time.time(),
                "token_id": signal.token_id,
                "side": signal.side,
            }
            self._save_positions()

            # Record in market_db
            try:
                market_db.record_trade(
                    strategy=STRATEGY_NAME,
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    side=signal.side,
                    price=fill_price,
                    size=signal.kelly_size,
                    edge=signal.edge,
                    confidence=signal.confidence,
                    reason=f"XGB prob={signal.model_prob:.3f}",
                )
            except Exception:
                pass

            self._total_trades += 1
            logger.info(
                f"[XGBOOST] Trade #{self._total_trades} filled @ {fill_price:.3f} "
                f"(target was {signal.contract_price:.3f})"
            )
            return True

        logger.info("[XGBOOST] Order not filled")
        return False

    def check_exits(self, shared_markets: list = None) -> list[dict]:
        """
        Check open XGBoost positions for exit conditions.

        Exit when:
        1. contract_price >= model_probability * EXIT_MULTIPLIER (0.9)
        2. days_to_expiry <= EXIT_DAYS_THRESHOLD (7)
        3. Max hold exceeded (14 days)

        Returns list of positions to exit with reason.
        """
        if not self._open_positions:
            return []

        exits = []
        market_map = {}
        if shared_markets:
            market_map = {m.id: m for m in shared_markets}

        for market_id, pos in list(self._open_positions.items()):
            try:
                m = market_map.get(market_id)
                if not m:
                    continue

                yes_price = m.prices.get("yes", 0.5)
                model_prob = pos["model_prob"]
                entry_time = pos["entry_time"]
                hold_days = (time.time() - entry_time) / 86400

                # Exit rule 1: price converged to model (take profit)
                if pos["side"] == "BUY_YES":
                    current_price = yes_price
                    exit_threshold = model_prob * EXIT_MULTIPLIER
                else:
                    current_price = 1.0 - yes_price
                    exit_threshold = (1.0 - model_prob) * EXIT_MULTIPLIER

                if current_price >= exit_threshold:
                    exits.append({
                        "market_id": market_id,
                        "token_id": pos["token_id"],
                        "reason": f"TP: price={current_price:.3f} >= threshold={exit_threshold:.3f}",
                        "side": pos["side"],
                    })
                    continue

                # Exit rule 2: approaching expiry
                days_left = self.feature_extractor._calc_days_to_expiry(m)
                if days_left <= EXIT_DAYS_THRESHOLD:
                    exits.append({
                        "market_id": market_id,
                        "token_id": pos["token_id"],
                        "reason": f"EXPIRY: {days_left:.0f} days left <= {EXIT_DAYS_THRESHOLD}",
                        "side": pos["side"],
                    })
                    continue

                # Exit rule 3: max hold exceeded
                if hold_days >= MAX_HOLD_DAYS:
                    exits.append({
                        "market_id": market_id,
                        "token_id": pos["token_id"],
                        "reason": f"MAX_HOLD: {hold_days:.0f} days >= {MAX_HOLD_DAYS}",
                        "side": pos["side"],
                    })
                    continue

            except Exception as e:
                logger.debug(f"[XGBOOST] Exit check error for {market_id[:16]}: {e}")

        if exits:
            logger.info(f"[XGBOOST] {len(exits)} exit signals: " +
                        ", ".join(e["reason"] for e in exits))

        return exits

    def execute_exit(self, exit_info: dict, api=None, risk=None,
                     live: bool = False) -> bool:
        """Execute a sell for an exit signal."""
        api = api or self.api
        risk = risk or self.risk

        market_id = exit_info["market_id"]
        token_id = exit_info["token_id"]

        logger.info(
            f"[XGBOOST] {'LIVE' if live else 'PAPER'} EXIT {market_id[:16]} — {exit_info['reason']}"
        )

        result = None
        if live and api:
            try:
                result = api.smart_sell(token_id=token_id, amount=0)  # Sell all
            except Exception as e:
                logger.error(f"[XGBOOST] Sell failed: {e}")
                return False
        else:
            result = {"status": "SOLD"}

        if result:
            # Remove from tracking
            if market_id in self._open_positions:
                del self._open_positions[market_id]
                self._save_positions()
            return True

        return False

    def _try_retrain(self):
        """Attempt to retrain the model."""
        try:
            features, labels = collect_training_data(
                db=market_db,
                feature_extractor=self.feature_extractor,
            )
            if len(features) >= MIN_TRAINING_SAMPLES:
                self.predictor.train(features, labels)
        except Exception as e:
            logger.warning(f"[XGBOOST] Retrain failed: {e}")

    def _log_prediction(self, market, model_prob: float, yes_price: float, action: str):
        """Log prediction for monitoring and future analysis."""
        try:
            entry = {
                "market_id": market.id,
                "question": (market.question[:60] if hasattr(market, 'question') else ""),
                "model_prob": round(model_prob, 4),
                "yes_price": round(yes_price, 4),
                "action": action,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._predictions_log.append(entry)
            # Keep last 1000
            if len(self._predictions_log) > 1000:
                self._predictions_log = self._predictions_log[-1000:]
            # Save periodically (every 50 predictions)
            if len(self._predictions_log) % 50 == 0:
                PREDICTIONS_LOG_PATH.write_text(
                    json.dumps(self._predictions_log[-500:], indent=2)
                )
        except Exception:
            pass

    def _save_positions(self):
        """Persist open positions to disk."""
        try:
            path = Path("logs/xgboost_positions.json")
            path.write_text(json.dumps(self._open_positions, indent=2))
        except Exception:
            pass

    def _load_positions(self):
        """Load open positions from disk."""
        try:
            path = Path("logs/xgboost_positions.json")
            if path.exists():
                self._open_positions = json.loads(path.read_text())
                logger.info(f"[XGBOOST] Loaded {len(self._open_positions)} open positions from disk")
        except Exception:
            self._open_positions = {}
