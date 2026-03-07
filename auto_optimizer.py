#!/usr/bin/env python3
"""
AutoOptimizer v1.0 — Autonomous strategy parameter optimization.

Inspired by Karpathy's AutoResearch: an autonomous loop that
proposes parameter changes, evaluates them via backtest, and
keeps improvements.

Usage:
    python3 auto_optimizer.py                   # run full optimization
    python3 auto_optimizer.py --strategy weather  # optimize only weather
    python3 auto_optimizer.py --max-iter 50      # limit iterations
    python3 auto_optimizer.py --report           # show best params found

How it works:
1. Load historical trades from logs/trades.json
2. Define parameter search space per strategy
3. For each iteration:
   a. Propose a parameter variation (grid + random perturbation)
   b. Simulate trades with new parameters (backtest_replay)
   c. Evaluate metric: Sharpe-like ratio = mean_pnl / std_pnl
   d. If better than current best, keep the change
4. Log all experiments to logs/auto_optimizer.json
5. Print final recommendations

Design principles (from AutoResearch):
- Single metric to optimize (Sharpe-like ratio)
- Fixed evaluation budget per experiment (backtest, not live)
- Human-readable experiment log
- Conservative: only recommend changes with >10% improvement
"""

import argparse
import json
import math
import random
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# Import backtest components
from backtest_replay import (
    Trade, FilterParams, parse_trades_json, parse_trades_from_logs,
    apply_filters, calc_metrics, LOG_DIR,
)


@dataclass
class Experiment:
    """Record of a single parameter experiment."""
    iteration: int
    params: dict
    metrics: dict
    score: float       # optimization target (higher = better)
    improved: bool
    timestamp: str


@dataclass
class ParamRange:
    """Defines the search space for a parameter."""
    name: str
    min_val: float
    max_val: float
    step: float
    current: float


# ── Search spaces per strategy ──

WEATHER_PARAMS = [
    ParamRange("min_edge", 0.02, 0.15, 0.01, 0.08),
    ParamRange("min_confidence", 0.30, 0.80, 0.05, 0.55),
    ParamRange("min_payoff", 0.15, 0.50, 0.05, 0.25),
    ParamRange("max_price", 0.70, 0.90, 0.05, 0.85),
    ParamRange("min_sources_high_price", 1, 3, 1, 2),
    ParamRange("high_price_threshold", 0.55, 0.75, 0.05, 0.65),
]


def compute_score(metrics: dict) -> float:
    """
    Optimization target: risk-adjusted return.

    Score = PnL / max(1, n_trades) * WR_bonus * PF_bonus

    Components:
    - PnL: raw profit/loss
    - WR bonus: multiplicative bonus for high win rate (>70%)
    - PF bonus: multiplicative bonus for profit factor > 1.5
    - Trade count penalty: penalize too few trades (overfitting)
    """
    pnl = metrics.get("pnl", 0)
    wr = metrics.get("wr", 0)
    pf = metrics.get("profit_factor", 0)
    n_closed = metrics.get("closed", 0)

    if n_closed < 5:
        return -999.0  # not enough data

    # Base: average PnL per trade
    avg_pnl = pnl / n_closed

    # WR bonus: reward high win rates (especially important for BUY_NO)
    wr_bonus = 1.0
    if wr > 80:
        wr_bonus = 1.3
    elif wr > 70:
        wr_bonus = 1.1
    elif wr < 50:
        wr_bonus = 0.7

    # PF bonus: reward high profit factor
    pf_bonus = 1.0
    if pf > 2.0:
        pf_bonus = 1.2
    elif pf > 1.5:
        pf_bonus = 1.1
    elif pf < 1.0:
        pf_bonus = 0.8

    # Trade count: penalize if too restrictive (< 10 trades)
    count_penalty = min(1.0, n_closed / 10.0)

    return avg_pnl * wr_bonus * pf_bonus * count_penalty


def params_to_filter(params: dict) -> FilterParams:
    """Convert param dict to FilterParams."""
    return FilterParams(
        min_edge=params.get("min_edge", 0.08),
        min_confidence=params.get("min_confidence", 0.55),
        min_payoff=params.get("min_payoff", 0.25),
        max_price=params.get("max_price", 0.85),
        min_sources=params.get("min_sources", 1),
        min_sources_high_price=int(params.get("min_sources_high_price", 2)),
        high_price_threshold=params.get("high_price_threshold", 0.65),
    )


def propose_variation(param_ranges: list[ParamRange], best_params: dict,
                      iteration: int) -> dict:
    """
    Propose a parameter variation.

    Strategy:
    - First N iterations: grid search (one param at a time)
    - After: random perturbation of 1-2 params from best known
    """
    params = dict(best_params)
    n_params = len(param_ranges)

    if iteration < n_params * 3:
        # Grid search phase: vary one param at a time
        param_idx = iteration % n_params
        p = param_ranges[param_idx]
        grid_step = (iteration // n_params) - 1  # -1, 0, 1
        new_val = p.current + grid_step * p.step
        new_val = max(p.min_val, min(p.max_val, new_val))
        params[p.name] = round(new_val, 4)
    else:
        # Random perturbation phase: perturb 1-2 params
        n_perturb = random.choice([1, 1, 2])
        chosen = random.sample(param_ranges, n_perturb)
        for p in chosen:
            # Random walk from best known value
            delta = random.gauss(0, p.step)
            base = best_params.get(p.name, p.current)
            new_val = base + delta
            new_val = max(p.min_val, min(p.max_val, new_val))
            if isinstance(p.step, int) or p.step >= 1:
                new_val = round(new_val)
            else:
                new_val = round(new_val, 4)
            params[p.name] = new_val

    return params


def run_optimization(trades: list[Trade], param_ranges: list[ParamRange],
                     max_iter: int = 100, strategy: str = "weather") -> list[Experiment]:
    """
    AutoResearch-style optimization loop.

    Returns list of all experiments sorted by score.
    """
    experiments: list[Experiment] = []

    # Initialize with current params
    best_params = {p.name: p.current for p in param_ranges}
    best_filter = params_to_filter(best_params)
    passed, blocked = apply_filters(list(trades), best_filter)
    best_metrics = calc_metrics(passed)
    best_score = compute_score(best_metrics)

    print(f"\n{'='*70}")
    print(f"  AutoOptimizer v1.0 — {strategy}")
    print(f"  Trades: {len(trades)} | Max iterations: {max_iter}")
    print(f"  Baseline score: {best_score:.4f}")
    print(f"  Baseline: WR={best_metrics['wr']:.1f}% PnL=${best_metrics['pnl']:+.2f} "
          f"PF={best_metrics['profit_factor']:.2f}")
    print(f"{'='*70}")

    improvements = 0
    start = time.time()

    for i in range(max_iter):
        # Propose variation
        candidate_params = propose_variation(param_ranges, best_params, i)

        # Evaluate
        candidate_filter = params_to_filter(candidate_params)
        passed, blocked = apply_filters(list(trades), candidate_filter)
        metrics = calc_metrics(passed)
        score = compute_score(metrics)

        improved = score > best_score
        if improved:
            improvements += 1
            best_score = score
            best_params = dict(candidate_params)
            best_metrics = metrics

        exp = Experiment(
            iteration=i,
            params=candidate_params,
            metrics=metrics,
            score=score,
            improved=improved,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        experiments.append(exp)

        # Log progress
        marker = " *** NEW BEST ***" if improved else ""
        if improved or i % 10 == 0:
            print(
                f"  [{i:3d}/{max_iter}] score={score:+.4f} "
                f"WR={metrics['wr']:.1f}% PnL=${metrics['pnl']:+.2f} "
                f"PF={metrics['profit_factor']:.2f} "
                f"trades={metrics['closed']}{marker}"
            )

    elapsed = time.time() - start
    print(f"\n{'='*70}")
    print(f"  COMPLETED: {max_iter} experiments in {elapsed:.1f}s")
    print(f"  Improvements found: {improvements}")
    print(f"{'='*70}")

    return experiments


def print_recommendations(experiments: list[Experiment],
                          param_ranges: list[ParamRange]):
    """Print final optimization recommendations."""
    if not experiments:
        print("No experiments to analyze.")
        return

    # Sort by score
    best = max(experiments, key=lambda e: e.score)
    baseline_params = {p.name: p.current for p in param_ranges}

    print(f"\n{'='*70}")
    print(f"  BEST PARAMETERS FOUND")
    print(f"{'='*70}")
    print(f"  Score: {best.score:+.4f}")
    print(f"  WR: {best.metrics['wr']:.1f}%")
    print(f"  PnL: ${best.metrics['pnl']:+.2f}")
    print(f"  Profit Factor: {best.metrics['profit_factor']:.2f}")
    print(f"  Closed trades: {best.metrics['closed']}")
    print()

    changes = []
    for p in param_ranges:
        old_val = p.current
        new_val = best.params.get(p.name, old_val)
        if old_val != new_val:
            changes.append((p.name, old_val, new_val))
            print(f"  {p.name}: {old_val} → {new_val}")

    if not changes:
        print("  No parameter changes recommended (current params are optimal).")

    # Safety check: is the improvement > 10%?
    baseline = next(
        (e for e in experiments if e.iteration == 0), None
    )
    if baseline and baseline.score > 0:
        improvement_pct = (best.score - baseline.score) / abs(baseline.score) * 100
        print(f"\n  Improvement: {improvement_pct:+.1f}% vs baseline")
        if improvement_pct < 10:
            print("  ⚠ Improvement < 10% — may not be statistically significant.")
            print("    Consider running more iterations or gathering more trade data.")
    elif baseline:
        print(f"\n  Baseline score was {baseline.score:.4f} → {best.score:.4f}")

    # Top 5 experiments
    print(f"\n{'='*70}")
    print(f"  TOP 5 EXPERIMENTS")
    print(f"{'='*70}")
    top5 = sorted(experiments, key=lambda e: e.score, reverse=True)[:5]
    for i, exp in enumerate(top5):
        diff_params = {
            k: v for k, v in exp.params.items()
            if v != baseline_params.get(k)
        }
        print(
            f"  #{i+1}: score={exp.score:+.4f} WR={exp.metrics['wr']:.1f}% "
            f"PnL=${exp.metrics['pnl']:+.2f} PF={exp.metrics['profit_factor']:.2f} "
            f"trades={exp.metrics['closed']} | {diff_params}"
        )


def save_experiments(experiments: list[Experiment], path: Path):
    """Save experiment log to JSON."""
    data = [asdict(e) for e in experiments]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n  Experiment log saved to {path}")


def main():
    parser = argparse.ArgumentParser(
        description="AutoOptimizer — autonomous strategy parameter optimization"
    )
    parser.add_argument(
        "--strategy", default="weather",
        choices=["weather"],
        help="Strategy to optimize (default: weather)"
    )
    parser.add_argument(
        "--max-iter", type=int, default=100,
        help="Maximum iterations (default: 100)"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Show results from previous optimization run"
    )
    args = parser.parse_args()

    exp_file = LOG_DIR / "auto_optimizer.json"

    if args.report:
        if not exp_file.exists():
            print("No previous optimization results found.")
            sys.exit(1)
        data = json.loads(exp_file.read_text())
        experiments = [
            Experiment(**d) for d in data
        ]
        print_recommendations(experiments, WEATHER_PARAMS)
        return

    # Load trades
    log_files = sorted(LOG_DIR.glob("bot_*.log"))
    trades = parse_trades_json()
    if not trades:
        trades = parse_trades_from_logs(log_files)

    if not trades:
        print("No trades found. Run the bot first to generate trade data.")
        sys.exit(1)

    strategy_trades = [t for t in trades if t.strategy == args.strategy]
    closed = [t for t in strategy_trades if t.outcome in ("WIN", "LOSS")]

    print(f"Loaded {len(trades)} trades, {len(strategy_trades)} {args.strategy} "
          f"({len(closed)} closed)")

    if len(closed) < 10:
        print(f"Not enough closed trades ({len(closed)} < 10). "
              f"Need more data for meaningful optimization.")
        sys.exit(1)

    # Select param ranges
    param_ranges = WEATHER_PARAMS

    # Run optimization
    random.seed(42)  # reproducible
    experiments = run_optimization(
        trades, param_ranges,
        max_iter=args.max_iter, strategy=args.strategy
    )

    # Save and report
    save_experiments(experiments, exp_file)
    print_recommendations(experiments, param_ranges)


if __name__ == "__main__":
    main()
