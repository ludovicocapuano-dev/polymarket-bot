#!/usr/bin/env python3
"""
Feature Analysis — standalone TSFresh + Prophet analysis on historical trades.

Outputs:
  1. Top 10 most predictive TSFresh features
  2. 5-day PnL forecast with uncertainty intervals
  3. 5-day Win Rate forecast
  4. Weekly seasonal patterns (day-of-week effects)

Usage:
    python3 scripts/feature_analysis.py                # full analysis
    python3 scripts/feature_analysis.py --tsfresh      # TSFresh only
    python3 scripts/feature_analysis.py --prophet      # Prophet only
    python3 scripts/feature_analysis.py --window 100   # use last 100 trades for TSFresh

Can be added to cron for periodic analysis:
    0 6 * * * cd /root/polymarket_toolkit && python3 scripts/feature_analysis.py >> logs/feature_analysis.log 2>&1
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

TRADES_PATH = Path("logs/trades.json")
REPORT_PATH = Path("logs/feature_analysis_report.json")


def load_trades() -> list[dict]:
    """Load trades from trades.json."""
    if not TRADES_PATH.exists():
        logger.error(f"No trades file at {TRADES_PATH}")
        return []
    try:
        with open(TRADES_PATH) as f:
            trades = json.load(f)
        logger.info(f"Loaded {len(trades)} trades from {TRADES_PATH}")
        return trades
    except Exception as e:
        logger.error(f"Failed to load trades: {e}")
        return []


def run_tsfresh_analysis(trades: list[dict], window: int = 50) -> dict:
    """Run TSFresh feature extraction and report top features."""
    from utils.feature_engine import extract_trade_features, get_top_features

    print("\n" + "=" * 60)
    print("  TSFresh Feature Extraction")
    print("=" * 60)

    if len(trades) < 10:
        print(f"  Not enough trades ({len(trades)}, need >= 10)")
        return {}

    # Force recompute for analysis
    features = extract_trade_features(trades, window=window, force=True)
    if not features:
        print("  No features extracted")
        return {}

    print(f"\n  Total features extracted: {len(features)}")

    # Top 10 by absolute value
    top = get_top_features(trades, n=10, window=window)
    print(f"\n  Top 10 features (by |value|):")
    print(f"  {'#':<4} {'Feature':<55} {'Value':>10}")
    print(f"  {'-'*4} {'-'*55} {'-'*10}")
    for i, (name, val) in enumerate(top, 1):
        print(f"  {i:<4} {name:<55} {val:>10.4f}")

    # Feature categories breakdown
    categories = {}
    for name in features:
        # tsfresh names: "kind__feature__param"
        parts = name.split("__")
        kind = parts[0] if parts else "unknown"
        categories[kind] = categories.get(kind, 0) + 1

    print(f"\n  Features by time series:")
    for kind, count in sorted(categories.items()):
        print(f"    {kind}: {count} features")

    return {"total_features": len(features), "top_10": top}


def run_prophet_analysis(trades: list[dict], periods: int = 5) -> dict:
    """Run Prophet forecasting and report results."""
    from utils.feature_engine import (
        forecast_pnl,
        forecast_win_rate,
        get_seasonal_components,
        trades_to_daily_pnl,
        trades_to_daily_wr,
    )

    print("\n" + "=" * 60)
    print("  Prophet Forecasting")
    print("=" * 60)

    results = {}

    # Daily PnL
    daily_pnl = trades_to_daily_pnl(trades)
    print(f"\n  Daily PnL data: {len(daily_pnl)} days")
    if len(daily_pnl) > 0:
        print(f"  Date range: {daily_pnl.index[0].date()} to {daily_pnl.index[-1].date()}")
        print(f"  Total PnL: ${daily_pnl.sum():.2f}")
        print(f"  Avg daily PnL: ${daily_pnl.mean():.2f}")

    # PnL forecast
    pnl_forecast = forecast_pnl(
        daily_pnl,
        periods=periods,
        save_plot=True,
        plot_path="logs/prophet_pnl_forecast.png",
    )
    if pnl_forecast is not None:
        print(f"\n  {periods}-Day PnL Forecast:")
        print(f"  {'Day':<12} {'Expected':>10} {'Lower':>10} {'Upper':>10}")
        print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10}")
        for _, row in pnl_forecast.iterrows():
            day_str = row["ds"].strftime("%Y-%m-%d")
            print(f"  {day_str:<12} ${row['yhat']:>9.2f} ${row['yhat_lower']:>9.2f} ${row['yhat_upper']:>9.2f}")

        total = pnl_forecast["yhat"].sum()
        total_low = pnl_forecast["yhat_lower"].sum()
        total_high = pnl_forecast["yhat_upper"].sum()
        print(f"  {'TOTAL':<12} ${total:>9.2f} ${total_low:>9.2f} ${total_high:>9.2f}")

        results["pnl_forecast"] = pnl_forecast.to_dict(orient="records")
    else:
        print("  PnL forecast: not enough data (need >= 14 days)")

    # Daily WR
    daily_wr = trades_to_daily_wr(trades)
    print(f"\n  Daily WR data: {len(daily_wr)} days")

    # WR forecast
    wr_forecast = forecast_win_rate(
        daily_wr,
        periods=periods,
        save_plot=True,
        plot_path="logs/prophet_wr_forecast.png",
    )
    if wr_forecast is not None:
        print(f"\n  {periods}-Day Win Rate Forecast:")
        print(f"  {'Day':<12} {'Expected':>10} {'Lower':>10} {'Upper':>10}")
        print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10}")
        for _, row in wr_forecast.iterrows():
            day_str = row["ds"].strftime("%Y-%m-%d")
            print(f"  {day_str:<12} {row['yhat']:>9.1%} {row['yhat_lower']:>9.1%} {row['yhat_upper']:>9.1%}")

        results["wr_forecast"] = wr_forecast.to_dict(orient="records")
    else:
        print("  WR forecast: not enough data (need >= 14 days)")

    # Seasonal patterns
    seasonal = get_seasonal_components(daily_pnl)
    if seasonal:
        print(f"\n  Weekly Seasonal Patterns (PnL effect):")
        print(f"  {'Day':<12} {'Effect':>10} {'Bar'}")
        print(f"  {'-'*12} {'-'*10} {'-'*20}")
        max_abs = max(abs(v) for v in seasonal.values()) or 1.0
        for day, effect in seasonal.items():
            bar_len = int(abs(effect) / max_abs * 15)
            bar_char = "+" if effect >= 0 else "-"
            bar = bar_char * bar_len
            print(f"  {day:<12} {effect:>+10.4f} {bar}")
        results["seasonal"] = seasonal
    else:
        print("  Seasonal analysis: not enough data")

    return results


def save_report(tsfresh_results: dict, prophet_results: dict):
    """Save analysis report to JSON."""
    report = {
        "timestamp": datetime.now().isoformat(),
        "tsfresh": tsfresh_results,
        "prophet": prophet_results,
    }
    try:
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nReport saved to {REPORT_PATH}")
    except Exception as e:
        logger.warning(f"Failed to save report: {e}")


def main():
    parser = argparse.ArgumentParser(description="TSFresh + Prophet feature analysis")
    parser.add_argument("--tsfresh", action="store_true", help="Run TSFresh only")
    parser.add_argument("--prophet", action="store_true", help="Run Prophet only")
    parser.add_argument("--window", type=int, default=50, help="TSFresh window size (default: 50)")
    parser.add_argument("--periods", type=int, default=5, help="Prophet forecast days (default: 5)")
    args = parser.parse_args()

    # Default: run both
    run_both = not args.tsfresh and not args.prophet

    os.chdir(Path(__file__).resolve().parent.parent)

    trades = load_trades()
    if not trades:
        print("No trades to analyze. Exiting.")
        sys.exit(1)

    print(f"\nAnalyzing {len(trades)} trades...")
    resolved = [t for t in trades if t.get("result") not in ("OPEN", None)]
    print(f"  Resolved: {len(resolved)}, Open: {len(trades) - len(resolved)}")

    tsfresh_results = {}
    prophet_results = {}

    if args.tsfresh or run_both:
        tsfresh_results = run_tsfresh_analysis(trades, window=args.window)

    if args.prophet or run_both:
        prophet_results = run_prophet_analysis(trades, periods=args.periods)

    save_report(tsfresh_results, prophet_results)

    print("\n" + "=" * 60)
    print("  Analysis complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
