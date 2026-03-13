"""
Feature Engine v1.0 — TSFresh + Prophet integration for Polymarket bot.

TSFresh: automatic time-series feature extraction from trade history.
Prophet: daily PnL and win-rate forecasting with weekly seasonality.

Usage:
    from utils.feature_engine import extract_trade_features, forecast_pnl, forecast_win_rate

Notes:
    - TSFresh uses MinimalFCParameters for speed (full extraction is 10-100x slower).
    - Prophet requires >= 14 days of daily data to fit weekly seasonality.
    - Feature extraction is cached: expensive computation runs at most every 200 trades.
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import pandas as pd
    import numpy as np
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    logger.warning("[FEATURE-ENGINE] pandas/numpy not available — disabled")

try:
    from tsfresh import extract_features
    from tsfresh.feature_extraction import MinimalFCParameters
    from tsfresh.utilities.dataframe_functions import impute
    HAS_TSFRESH = True
except ImportError:
    HAS_TSFRESH = False
    logger.info("[FEATURE-ENGINE] tsfresh not available — TSFresh features disabled")

try:
    from prophet import Prophet
    HAS_PROPHET = True
except ImportError:
    HAS_PROPHET = False
    logger.info("[FEATURE-ENGINE] prophet not available — forecasting disabled")

try:
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


# ── Cache ─────────────────────────────────────────────────────

CACHE_DIR = Path("logs")
TSFRESH_CACHE_PATH = CACHE_DIR / "tsfresh_features_cache.json"
TSFRESH_MIN_INTERVAL = 200  # minimum trades between re-extractions


class _TSFreshCache:
    """In-memory + disk cache for TSFresh features."""

    def __init__(self):
        self._features: dict | None = None
        self._trade_count_at_extraction: int = 0
        self._last_extraction_time: float = 0.0
        self._load()

    def _load(self):
        try:
            if TSFRESH_CACHE_PATH.exists():
                with open(TSFRESH_CACHE_PATH) as f:
                    data = json.load(f)
                self._features = data.get("features")
                self._trade_count_at_extraction = data.get("trade_count", 0)
                self._last_extraction_time = data.get("timestamp", 0.0)
                logger.info(
                    f"[TSFRESH] Cache loaded: {len(self._features or {})} features, "
                    f"extracted at {self._trade_count_at_extraction} trades"
                )
        except Exception as e:
            logger.debug(f"[TSFRESH] Cache load failed: {e}")

    def save(self):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(TSFRESH_CACHE_PATH, "w") as f:
                json.dump({
                    "features": self._features,
                    "trade_count": self._trade_count_at_extraction,
                    "timestamp": self._last_extraction_time,
                }, f)
        except Exception as e:
            logger.warning(f"[TSFRESH] Cache save failed: {e}")

    def should_recompute(self, current_trade_count: int) -> bool:
        if self._features is None:
            return True
        return (current_trade_count - self._trade_count_at_extraction) >= TSFRESH_MIN_INTERVAL

    def get(self) -> dict | None:
        return self._features

    def set(self, features: dict, trade_count: int):
        self._features = features
        self._trade_count_at_extraction = trade_count
        self._last_extraction_time = time.time()
        self.save()


_tsfresh_cache = _TSFreshCache()


# ── TSFresh Feature Extraction ────────────────────────────────

def _build_time_series_df(trades: list[dict]) -> pd.DataFrame:
    """
    Build a tsfresh-compatible DataFrame from trade records.

    Creates three time series per trade sequence:
      - pnl: cumulative PnL over trade index
      - edge: estimated edge at trade time
      - price: entry price

    tsfresh expects columns: [id, time, value]
    We use a single id=0 (one entity) with trade index as time.
    """
    rows = []
    cum_pnl = 0.0

    for i, t in enumerate(trades):
        pnl = float(t.get("pnl", 0.0))
        edge = float(t.get("edge", 0.0))
        price = float(t.get("price", 0.0))
        cum_pnl += pnl

        # Series: cumulative PnL
        rows.append({"id": 0, "time": i, "kind": "cum_pnl", "value": cum_pnl})
        # Series: edge
        rows.append({"id": 0, "time": i, "kind": "edge", "value": edge})
        # Series: price
        rows.append({"id": 0, "time": i, "kind": "price", "value": price})

    return pd.DataFrame(rows)


def extract_trade_features(
    trades: list[dict],
    window: int = 50,
    force: bool = False,
) -> dict:
    """
    Extract TSFresh features from recent trades.

    Args:
        trades: list of trade dicts with keys: time, pnl, edge, price, result, strategy, ...
        window: number of recent trades to use (default 50).
        force: bypass cache and recompute.

    Returns:
        dict of {feature_name: float} that can augment the meta-labeler's feature vector.
        Returns empty dict if not enough data or tsfresh unavailable.
    """
    if not HAS_TSFRESH or not HAS_PANDAS:
        return {}

    if len(trades) < 10:
        logger.debug("[TSFRESH] Not enough trades for feature extraction (need >= 10)")
        return {}

    # Check cache
    if not force and not _tsfresh_cache.should_recompute(len(trades)):
        cached = _tsfresh_cache.get()
        if cached is not None:
            return cached

    logger.info(f"[TSFRESH] Extracting features from {min(window, len(trades))} recent trades...")
    start_time = time.time()

    try:
        recent = trades[-window:]
        ts_df = _build_time_series_df(recent)

        # Use MinimalFCParameters for speed (30-50 features vs 800+ for full)
        features_df = extract_features(
            ts_df,
            column_id="id",
            column_sort="time",
            column_kind="kind",
            column_value="value",
            default_fc_parameters=MinimalFCParameters(),
            disable_progressbar=True,
            n_jobs=1,  # single-threaded to avoid overhead on small data
        )

        # Impute NaN/Inf
        impute(features_df)

        # Convert to dict: {feature_name: value}
        if features_df.empty:
            logger.warning("[TSFRESH] No features extracted")
            return {}

        feature_dict = features_df.iloc[0].to_dict()

        # Filter out features with zero variance (constant) — not useful
        feature_dict = {
            k: float(v) for k, v in feature_dict.items()
            if not (np.isnan(v) or np.isinf(v))
        }

        elapsed = time.time() - start_time
        logger.info(
            f"[TSFRESH] Extracted {len(feature_dict)} features in {elapsed:.1f}s "
            f"from {len(recent)} trades"
        )

        # Cache results
        _tsfresh_cache.set(feature_dict, len(trades))
        return feature_dict

    except Exception as e:
        logger.warning(f"[TSFRESH] Feature extraction failed: {e}")
        return {}


def get_top_features(
    trades: list[dict],
    n: int = 10,
    window: int = 50,
) -> list[tuple[str, float]]:
    """
    Get top N features by absolute value (most variable/interesting).

    Returns list of (feature_name, value) tuples sorted by |value| descending.
    """
    features = extract_trade_features(trades, window=window)
    if not features:
        return []

    sorted_feats = sorted(features.items(), key=lambda x: abs(x[1]), reverse=True)
    return sorted_feats[:n]


# ── Prophet Forecasting ───────────────────────────────────────

def _prepare_prophet_df(daily_series: pd.Series) -> pd.DataFrame:
    """
    Convert a daily pd.Series (index=date, values=metric) to Prophet format.
    Prophet expects columns: ds (datetime), y (value).
    """
    df = pd.DataFrame({
        "ds": pd.to_datetime(daily_series.index),
        "y": daily_series.values.astype(float),
    })
    df = df.dropna().sort_values("ds").reset_index(drop=True)
    return df


def forecast_pnl(
    daily_pnl: pd.Series,
    periods: int = 5,
    save_plot: bool = True,
    plot_path: str = "logs/prophet_pnl_forecast.png",
) -> pd.DataFrame | None:
    """
    Forecast daily PnL for the next N days using Prophet.

    Args:
        daily_pnl: pd.Series with date index and daily PnL values.
        periods: number of days to forecast (default 5).
        save_plot: whether to save forecast plot to disk.
        plot_path: path for the forecast plot.

    Returns:
        pd.DataFrame with columns [ds, yhat, yhat_lower, yhat_upper] for forecast period.
        Returns None if not enough data or Prophet unavailable.
    """
    if not HAS_PROPHET or not HAS_PANDAS:
        logger.info("[PROPHET] Not available — skipping PnL forecast")
        return None

    if len(daily_pnl) < 14:
        logger.info(
            f"[PROPHET] Not enough daily data for PnL forecast "
            f"({len(daily_pnl)} days, need >= 14)"
        )
        return None

    try:
        df = _prepare_prophet_df(daily_pnl)

        model = Prophet(
            daily_seasonality=False,
            weekly_seasonality=True,   # weekends = fewer weather markets
            yearly_seasonality=False,  # not enough data for yearly
            changepoint_prior_scale=0.1,  # conservative: avoid overfitting
            interval_width=0.80,
        )
        # Suppress Prophet's verbose logging
        model.fit(df)

        future = model.make_future_dataframe(periods=periods)
        forecast = model.predict(future)

        # Save plot
        if save_plot and HAS_MATPLOTLIB:
            try:
                fig = model.plot(forecast)
                fig.suptitle("Daily PnL Forecast (Prophet)", fontsize=14)
                os.makedirs(os.path.dirname(plot_path) or ".", exist_ok=True)
                fig.savefig(plot_path, dpi=100, bbox_inches="tight")
                plt.close(fig)
                logger.info(f"[PROPHET] PnL forecast plot saved to {plot_path}")
            except Exception as e:
                logger.debug(f"[PROPHET] Plot save failed: {e}")

        # Return only future rows
        forecast_future = forecast.tail(periods)[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy()
        forecast_future = forecast_future.reset_index(drop=True)

        total_expected = forecast_future["yhat"].sum()
        logger.info(
            f"[PROPHET] PnL forecast: next {periods} days expected total=${total_expected:.2f}, "
            f"range=[${forecast_future['yhat_lower'].sum():.2f}, "
            f"${forecast_future['yhat_upper'].sum():.2f}]"
        )

        return forecast_future

    except Exception as e:
        logger.warning(f"[PROPHET] PnL forecast failed: {e}")
        return None


def forecast_win_rate(
    daily_wr: pd.Series,
    periods: int = 5,
    save_plot: bool = True,
    plot_path: str = "logs/prophet_wr_forecast.png",
) -> pd.DataFrame | None:
    """
    Forecast daily win rate for the next N days using Prophet.

    Args:
        daily_wr: pd.Series with date index and daily win rate values (0-1).
        periods: number of days to forecast (default 5).
        save_plot: whether to save forecast plot to disk.
        plot_path: path for the forecast plot.

    Returns:
        pd.DataFrame with columns [ds, yhat, yhat_lower, yhat_upper].
        Returns None if not enough data.
    """
    if not HAS_PROPHET or not HAS_PANDAS:
        return None

    if len(daily_wr) < 14:
        logger.info(
            f"[PROPHET] Not enough daily data for WR forecast "
            f"({len(daily_wr)} days, need >= 14)"
        )
        return None

    try:
        df = _prepare_prophet_df(daily_wr)

        model = Prophet(
            daily_seasonality=False,
            weekly_seasonality=True,
            yearly_seasonality=False,
            changepoint_prior_scale=0.05,  # very conservative for win rate
            interval_width=0.80,
        )
        model.fit(df)

        future = model.make_future_dataframe(periods=periods)
        forecast = model.predict(future)

        # Clip to [0, 1] — win rate can't be outside this range
        forecast["yhat"] = forecast["yhat"].clip(0, 1)
        forecast["yhat_lower"] = forecast["yhat_lower"].clip(0, 1)
        forecast["yhat_upper"] = forecast["yhat_upper"].clip(0, 1)

        if save_plot and HAS_MATPLOTLIB:
            try:
                fig = model.plot(forecast)
                fig.suptitle("Daily Win Rate Forecast (Prophet)", fontsize=14)
                os.makedirs(os.path.dirname(plot_path) or ".", exist_ok=True)
                fig.savefig(plot_path, dpi=100, bbox_inches="tight")
                plt.close(fig)
                logger.info(f"[PROPHET] WR forecast plot saved to {plot_path}")
            except Exception as e:
                logger.debug(f"[PROPHET] Plot save failed: {e}")

        forecast_future = forecast.tail(periods)[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy()
        forecast_future = forecast_future.reset_index(drop=True)

        avg_wr = forecast_future["yhat"].mean()
        logger.info(
            f"[PROPHET] WR forecast: next {periods} days avg={avg_wr:.1%}, "
            f"range=[{forecast_future['yhat_lower'].mean():.1%}, "
            f"{forecast_future['yhat_upper'].mean():.1%}]"
        )

        return forecast_future

    except Exception as e:
        logger.warning(f"[PROPHET] WR forecast failed: {e}")
        return None


def get_seasonal_components(
    daily_pnl: pd.Series,
) -> dict | None:
    """
    Extract weekly seasonal components from daily PnL.

    Returns dict with day-of-week effects:
        {"Monday": float, "Tuesday": float, ..., "Sunday": float}
    Positive = above average, negative = below average.
    """
    if not HAS_PROPHET or not HAS_PANDAS:
        return None

    if len(daily_pnl) < 14:
        return None

    try:
        df = _prepare_prophet_df(daily_pnl)

        model = Prophet(
            daily_seasonality=False,
            weekly_seasonality=True,
            yearly_seasonality=False,
        )
        model.fit(df)

        # Generate a full week of predictions to extract seasonality
        future = model.make_future_dataframe(periods=7)
        forecast = model.predict(future)

        # Extract weekly component for each day of week
        forecast["dow"] = forecast["ds"].dt.day_name()
        weekly = forecast.groupby("dow")["weekly"].mean()

        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        result = {}
        for day in day_order:
            result[day] = round(float(weekly.get(day, 0.0)), 4)

        logger.info(f"[PROPHET] Weekly seasonality: {result}")
        return result

    except Exception as e:
        logger.debug(f"[PROPHET] Seasonal extraction failed: {e}")
        return None


# ── Helper: trades.json → daily aggregates ────────────────────

def trades_to_daily_pnl(trades: list[dict]) -> pd.Series:
    """
    Aggregate trade list into daily PnL series.

    Args:
        trades: list of trade dicts with 'time' and 'pnl' keys.

    Returns:
        pd.Series with date index and daily PnL sum.
    """
    if not HAS_PANDAS:
        return pd.Series(dtype=float)

    records = []
    for t in trades:
        ts = t.get("time", "")
        pnl = float(t.get("pnl", 0.0))
        if ts and t.get("result") not in ("OPEN", None):
            records.append({"date": str(ts)[:10], "pnl": pnl})

    if not records:
        return pd.Series(dtype=float)

    df = pd.DataFrame(records)
    daily = df.groupby("date")["pnl"].sum()
    daily.index = pd.to_datetime(daily.index)
    return daily.sort_index()


def trades_to_daily_wr(trades: list[dict]) -> pd.Series:
    """
    Aggregate trade list into daily win rate series.

    Args:
        trades: list of trade dicts with 'time' and 'result' keys.

    Returns:
        pd.Series with date index and daily win rate (0-1).
    """
    if not HAS_PANDAS:
        return pd.Series(dtype=float)

    records = []
    for t in trades:
        ts = t.get("time", "")
        result = t.get("result", "")
        if ts and result in ("WIN", "LOSS"):
            records.append({"date": str(ts)[:10], "won": 1 if result == "WIN" else 0})

    if not records:
        return pd.Series(dtype=float)

    df = pd.DataFrame(records)
    daily = df.groupby("date")["won"].mean()
    daily.index = pd.to_datetime(daily.index)
    return daily.sort_index()
