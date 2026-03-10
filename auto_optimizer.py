#!/usr/bin/env python3
"""
AutoOptimizer v2.0 — Autonomous multi-strategy parameter optimization.

Inspired by Karpathy's AutoResearch: an autonomous loop that
proposes parameter changes, evaluates them via backtest, and
keeps improvements. Auto-applies when improvement is significant.

Usage:
    python3 auto_optimizer.py                          # optimize all strategies
    python3 auto_optimizer.py --strategy weather       # optimize only weather
    python3 auto_optimizer.py --strategy all           # all strategies sequentially
    python3 auto_optimizer.py --max-iter 200           # limit iterations
    python3 auto_optimizer.py --report                 # show best params found
    python3 auto_optimizer.py --auto-apply             # auto-apply if >15% improvement

How it works:
1. Load historical trades from bot logs + trades.json
2. Define parameter search space per strategy
3. For each iteration:
   a. Propose a parameter variation (grid + random perturbation)
   b. Simulate trades with new parameters (backtest_replay)
   c. Evaluate metric: risk-adjusted return
   d. If better than current best, keep the change
4. Log all experiments to logs/auto_optimizer_{strategy}.json
5. Auto-apply if improvement >15% and >50 closed trades (--auto-apply)
6. Print final recommendations

Design principles (from AutoResearch):
- Single metric to optimize (risk-adjusted avg PnL)
- Fixed evaluation budget per experiment (backtest, not live)
- Human-readable experiment log
- Conservative: only auto-apply with >15% improvement + >50 closed trades
"""

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, asdict, field
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
    strategy: str = ""


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
    # Core filters
    ParamRange("min_edge", 0.02, 0.15, 0.01, 0.08),
    ParamRange("min_confidence", 0.30, 0.80, 0.05, 0.55),
    ParamRange("min_payoff", 0.10, 0.50, 0.05, 0.25),
    ParamRange("max_price", 0.70, 0.95, 0.05, 0.85),
    ParamRange("min_sources_high_price", 1, 3, 1, 2),
    ParamRange("high_price_threshold", 0.45, 0.80, 0.05, 0.65),
    # v2.0: Expanded search space — horizon-specific edges
    ParamRange("min_edge_same_day", 0.02, 0.10, 0.01, 0.05),
    ParamRange("min_edge_1d", 0.05, 0.20, 0.01, 0.12),
    ParamRange("min_edge_2d", 0.10, 0.30, 0.02, 0.20),
    # v2.0: Sizing params
    ParamRange("ev_minimum", 0.03, 0.20, 0.02, 0.10),
    ParamRange("meta_label_threshold", 0.25, 0.60, 0.05, 0.40),
    # v2.0: City tier caps
    ParamRange("city_tier2_max_bet", 10, 40, 5, 25),
    ParamRange("max_weather_bet", 30, 100, 10, 60),
]

FAVORITE_LONGSHOT_PARAMS = [
    ParamRange("min_price", 0.60, 0.80, 0.05, 0.70),
    ParamRange("max_price", 0.80, 0.95, 0.05, 0.90),
    ParamRange("base_alpha", 1.05, 1.30, 0.02, 1.12),
    ParamRange("min_edge", 0.005, 0.03, 0.005, 0.01),
    ParamRange("min_volume", 25000, 100000, 10000, 50000),
    ParamRange("max_bet", 20, 75, 5, 40),
    ParamRange("kelly_fraction", 0.25, 0.75, 0.05, 0.50),
]

ABANDONED_POSITION_PARAMS = [
    ParamRange("min_near_certain_price", 0.85, 0.97, 0.02, 0.94),
    ParamRange("max_near_certain_price", 0.95, 0.999, 0.01, 0.99),
    ParamRange("max_volume_24h", 250, 1000, 50, 500),
    ParamRange("max_hours_to_resolution", 24, 72, 6, 48),
    ParamRange("min_hours_to_resolution", 0.5, 6, 0.5, 1.0),
    ParamRange("max_position", 25, 100, 5, 50),
]

STRATEGY_PARAMS = {
    "weather": WEATHER_PARAMS,
    "favorite_longshot": FAVORITE_LONGSHOT_PARAMS,
    "abandoned_position": ABANDONED_POSITION_PARAMS,
}

# Auto-apply thresholds
AUTO_APPLY_MIN_IMPROVEMENT = 15.0   # percent
AUTO_APPLY_MIN_CLOSED = 50          # trades


def compute_score(metrics: dict, strategy: str = "weather",
                   params: dict = None, param_ranges: list = None) -> float:
    """
    Optimization target: risk-adjusted return.

    v2.2 (Karpathy simplicity criterion): adds simplicity_factor that
    penalizes params drifting far from defaults. Occam's razor: if two
    configs score similarly, prefer the one closer to defaults.
    Score = avg_pnl * wr_factor * pf_factor * volume_penalty * simplicity_factor
    """
    pnl = metrics.get("pnl", 0)
    wr = metrics.get("wr", 0)
    pf = metrics.get("profit_factor", 0)
    n_closed = metrics.get("closed", 0)

    if n_closed < 5:
        return -999.0  # not enough data

    # Base: average PnL per trade
    avg_pnl = pnl / n_closed

    # Continuous WR factor (centered at 65%, scales 0.7x–1.4x)
    wr_factor = 0.7 + 0.7 * max(0, min(1, (wr - 40) / 50))

    # Continuous PF factor (centered at 1.5, scales 0.75x–1.3x)
    if pf == float("inf"):
        pf_factor = 1.3
    else:
        pf_factor = 0.75 + 0.55 * max(0, min(1, (pf - 0.5) / 2.5))

    # v2.1: sqrt volume penalty
    volume_penalty = min(1.0, math.sqrt(n_closed / 30.0))

    # v2.2: Karpathy simplicity criterion — penalize drift from defaults
    # Each param that deviates >2 steps from default costs 1% per extra step
    simplicity_factor = 1.0
    if params and param_ranges:
        total_drift = 0
        for p in param_ranges:
            if p.name in params and p.step > 0:
                drift_steps = abs(params[p.name] - p.current) / p.step
                if drift_steps > 2:
                    total_drift += drift_steps - 2
        # Cap at 15% penalty max (0.85x)
        simplicity_factor = max(0.85, 1.0 - total_drift * 0.01)

    return avg_pnl * wr_factor * pf_factor * volume_penalty * simplicity_factor


def params_to_filter(params: dict, strategy: str = "weather") -> FilterParams:
    """Convert param dict to FilterParams (weather-specific)."""
    return FilterParams(
        min_edge=params.get("min_edge", 0.08),
        min_confidence=params.get("min_confidence", 0.55),
        min_payoff=params.get("min_payoff", 0.25),
        max_price=params.get("max_price", 0.85),
        min_sources=params.get("min_sources", 1),
        min_sources_high_price=int(params.get("min_sources_high_price", 2)),
        high_price_threshold=params.get("high_price_threshold", 0.65),
    )


def apply_strategy_filters(trades: list[Trade], params: dict,
                           strategy: str) -> tuple[list[Trade], list[Trade]]:
    """Apply filters for any strategy (not just weather)."""
    if strategy == "weather":
        return apply_filters(trades, params_to_filter(params, strategy))

    # Generic edge/price filter for other strategies
    passed = []
    blocked = []
    for t in trades:
        if t.strategy != strategy:
            passed.append(t)
            continue

        block = False

        # Edge filter
        min_edge = params.get("min_edge", 0)
        if min_edge > 0 and t.edge > 0 and t.edge < min_edge:
            block = True

        # Price range filter (favorite_longshot)
        min_price = params.get("min_price", 0)
        max_price = params.get("max_price", 1.0)
        if min_price > 0 and t.price > 0 and t.price < min_price:
            block = True
        if max_price < 1.0 and t.price > 0 and t.price > max_price:
            block = True

        # Near-certain price (abandoned_position)
        min_nc = params.get("min_near_certain_price", 0)
        max_nc = params.get("max_near_certain_price", 1.0)
        if min_nc > 0 and t.price > 0 and t.price < min_nc:
            block = True
        if max_nc < 1.0 and t.price > 0 and t.price > max_nc:
            block = True

        if block:
            blocked.append(t)
        else:
            passed.append(t)

    return passed, blocked


def calc_strategy_metrics(trades: list[Trade], strategy: str) -> dict:
    """Calculate metrics for any strategy."""
    strat = [t for t in trades if t.strategy == strategy]
    wins = [t for t in strat if t.outcome == "WIN"]
    losses = [t for t in strat if t.outcome == "LOSS"]
    closed = wins + losses

    total_pnl = sum(t.pnl for t in strat if t.pnl != 0)
    gross_wins = sum(t.pnl for t in wins if t.pnl > 0)
    gross_losses = abs(sum(t.pnl for t in losses if t.pnl < 0))

    return {
        "total": len(strat),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "wr": len(wins) / len(closed) * 100 if closed else 0,
        "pnl": total_pnl,
        "gross_wins": gross_wins,
        "gross_losses": gross_losses,
        "profit_factor": gross_wins / gross_losses if gross_losses > 0 else float("inf"),
        "avg_pnl": total_pnl / len(closed) if closed else 0,
    }


def propose_variation(param_ranges: list[ParamRange], best_params: dict,
                      iteration: int) -> dict:
    """
    Propose a parameter variation.

    Strategy:
    - First N*3 iterations: grid search (one param at a time)
    - After: random perturbation of 1-3 params from best known
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
        # Random perturbation phase: perturb 1-3 params
        n_perturb = random.choice([1, 1, 2, 2, 3])
        chosen = random.sample(param_ranges, min(n_perturb, len(param_ranges)))
        for p in chosen:
            # Random walk from best known value
            delta = random.gauss(0, p.step * 1.5)  # wider search
            base = best_params.get(p.name, p.current)
            new_val = base + delta
            new_val = max(p.min_val, min(p.max_val, new_val))
            if isinstance(p.step, int) or p.step >= 1:
                new_val = round(new_val)
            else:
                new_val = round(new_val, 4)
            params[p.name] = new_val

    return params


def split_trades_temporal(trades: list[Trade], train_ratio: float = 0.70
                          ) -> tuple[list[Trade], list[Trade]]:
    """
    v2.1: Split trades temporally (70/30) for out-of-sample validation.
    Sorts by timestamp, uses first 70% for training, last 30% for testing.
    """
    sorted_trades = sorted(trades, key=lambda t: t.timestamp)
    split_idx = int(len(sorted_trades) * train_ratio)
    return sorted_trades[:split_idx], sorted_trades[split_idx:]


def eval_params(trades: list[Trade], params: dict, strategy: str) -> dict:
    """Evaluate parameters on a set of trades, return metrics."""
    if strategy == "weather":
        passed, blocked = apply_filters(list(trades), params_to_filter(params))
        return calc_metrics(passed)
    else:
        passed, blocked = apply_strategy_filters(list(trades), params, strategy)
        return calc_strategy_metrics(passed, strategy)


def run_optimization(trades: list[Trade], param_ranges: list[ParamRange],
                     max_iter: int = 100, strategy: str = "weather") -> list[Experiment]:
    """AutoResearch-style optimization loop with temporal train/test split."""
    experiments: list[Experiment] = []

    # v2.1: temporal train/test split for out-of-sample validation
    strat_trades = [t for t in trades if t.strategy == strategy]
    has_test = len(strat_trades) >= 30  # need enough for split
    if has_test:
        train_trades, test_trades = split_trades_temporal(trades, 0.70)
        train_strat = [t for t in train_trades if t.strategy == strategy]
        test_strat = [t for t in test_trades if t.strategy == strategy]
        print(f"  Train/Test split: {len(train_strat)}/{len(test_strat)} "
              f"{strategy} trades (70/30 temporal)")
    else:
        train_trades = trades
        test_trades = []
        print(f"  No train/test split (<30 trades), using full dataset")

    # Initialize with current params
    best_params = {p.name: p.current for p in param_ranges}
    best_metrics = eval_params(train_trades, best_params, strategy)
    best_score = compute_score(best_metrics, strategy, best_params, param_ranges)

    print(f"\n{'='*70}")
    print(f"  AutoOptimizer v2.2 — {strategy}")
    print(f"  Trades: {len(trades)} | Max iterations: {max_iter}")
    print(f"  Params: {len(param_ranges)} searchable")
    print(f"  Baseline score: {best_score:.4f}")
    print(f"  Baseline: WR={best_metrics['wr']:.1f}% PnL=${best_metrics['pnl']:+.2f} "
          f"PF={best_metrics['profit_factor']:.2f}")
    print(f"{'='*70}")

    improvements = 0
    start = time.time()

    for i in range(max_iter):
        candidate_params = propose_variation(param_ranges, best_params, i)
        metrics = eval_params(train_trades, candidate_params, strategy)
        score = compute_score(metrics, strategy, candidate_params, param_ranges)

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
            strategy=strategy,
        )
        experiments.append(exp)

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

    # v2.1: Out-of-sample validation on test set
    if has_test and test_trades:
        test_metrics = eval_params(test_trades, best_params, strategy)
        test_score = compute_score(test_metrics, strategy, best_params, param_ranges)
        print(f"\n  OUT-OF-SAMPLE (test set):")
        print(f"    Score: {test_score:+.4f} (train: {best_score:+.4f})")
        print(f"    WR: {test_metrics['wr']:.1f}% | PnL: ${test_metrics['pnl']:+.2f} | "
              f"PF: {test_metrics['profit_factor']:.2f} | "
              f"Trades: {test_metrics['closed']}")
        if best_score > 0 and test_score < best_score * 0.5:
            print(f"    ⚠ WARNING: test score <50% of train — possible overfitting!")

    print(f"{'='*70}")

    return experiments


def print_recommendations(experiments: list[Experiment],
                          param_ranges: list[ParamRange],
                          strategy: str = "weather"):
    """Print final optimization recommendations."""
    if not experiments:
        print("No experiments to analyze.")
        return

    best = max(experiments, key=lambda e: e.score)
    baseline_params = {p.name: p.current for p in param_ranges}

    print(f"\n{'='*70}")
    print(f"  BEST PARAMETERS FOUND — {strategy}")
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

    baseline = next((e for e in experiments if e.iteration == 0), None)
    improvement_pct = 0
    if baseline and baseline.score > 0:
        improvement_pct = (best.score - baseline.score) / abs(baseline.score) * 100
        print(f"\n  Improvement: {improvement_pct:+.1f}% vs baseline")
        if improvement_pct < 10:
            print("  ⚠ Improvement < 10% — may not be statistically significant.")
    elif baseline:
        print(f"\n  Baseline score was {baseline.score:.4f} → {best.score:.4f}")

    # Top 5
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

    return improvement_pct, changes, best


def auto_apply_params(strategy: str, changes: list, best: Experiment,
                      improvement_pct: float):
    """
    Auto-apply optimized parameters to the live config.

    Writes to logs/auto_optimizer_applied_{strategy}.json so the bot
    can pick up new params on next cycle.
    """
    n_closed = best.metrics.get("closed", 0)

    if improvement_pct < AUTO_APPLY_MIN_IMPROVEMENT:
        print(f"\n  ⏭ Skip auto-apply: improvement {improvement_pct:.1f}% < "
              f"{AUTO_APPLY_MIN_IMPROVEMENT}% threshold")
        return False

    if n_closed < AUTO_APPLY_MIN_CLOSED:
        print(f"\n  ⏭ Skip auto-apply: only {n_closed} closed trades < "
              f"{AUTO_APPLY_MIN_CLOSED} minimum")
        return False

    if not changes:
        print("\n  ⏭ Skip auto-apply: no parameter changes.")
        return False

    # Write applied params
    applied = {
        "strategy": strategy,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "improvement_pct": round(improvement_pct, 2),
        "closed_trades": n_closed,
        "score": best.score,
        "metrics": best.metrics,
        "params": best.params,
        "changes": [
            {"name": name, "old": old, "new": new}
            for name, old, new in changes
        ],
    }

    applied_file = LOG_DIR / f"auto_optimizer_applied_{strategy}.json"
    # Append to history
    history = []
    if applied_file.exists():
        try:
            history = json.loads(applied_file.read_text())
            if not isinstance(history, list):
                history = [history]
        except Exception:
            history = []

    history.append(applied)
    with open(applied_file, "w") as f:
        json.dump(history, f, indent=2)

    print(f"\n  ✅ AUTO-APPLIED: {len(changes)} parameter(s) for {strategy}")
    print(f"     Improvement: {improvement_pct:+.1f}% | Trades: {n_closed}")
    print(f"     Saved to: {applied_file}")
    for name, old, new in changes:
        print(f"     {name}: {old} → {new}")

    return True


def save_experiments(experiments: list[Experiment], path: Path):
    """Save experiment log to JSON."""
    data = [asdict(e) for e in experiments]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n  Experiment log saved to {path}")


def save_results_tsv(experiments: list[Experiment], strategy: str):
    """
    v2.2 (Karpathy): Human-readable TSV log of all experiments.
    Append-only — accumulates across runs for easy review.
    """
    tsv_path = LOG_DIR / f"results_{strategy}.tsv"
    write_header = not tsv_path.exists()
    with open(tsv_path, "a") as f:
        if write_header:
            f.write("timestamp\titeration\tscore\twr\tpnl\tpf\ttrades\tstatus\tchanges\n")
        for exp in experiments:
            m = exp.metrics
            status = "keep" if exp.improved else "discard"
            # Show which params differ from defaults
            changed = {k: v for k, v in exp.params.items()
                       if any(p.name == k and p.current != v
                              for p in STRATEGY_PARAMS.get(strategy, []))}
            changes_str = " ".join(f"{k}={v}" for k, v in changed.items()) or "-"
            f.write(
                f"{exp.timestamp}\t{exp.iteration}\t{exp.score:+.4f}\t"
                f"{m.get('wr', 0):.1f}\t{m.get('pnl', 0):+.2f}\t"
                f"{m.get('profit_factor', 0):.2f}\t{m.get('closed', 0)}\t"
                f"{status}\t{changes_str}\n"
            )
    print(f"  Results TSV appended to {tsv_path}")


def load_trades():
    """Load and merge trades from all sources."""
    log_files = sorted(LOG_DIR.glob("bot_*.log"))
    trades = parse_trades_json()
    log_trades = parse_trades_from_logs(log_files)
    if log_trades:
        existing_ts = {(t.timestamp, t.strategy, t.price) for t in trades}
        added = 0
        for t in log_trades:
            key = (t.timestamp, t.strategy, t.price)
            if key not in existing_ts:
                trades.append(t)
                existing_ts.add(key)
                added += 1
        if added:
            print(f"  + {added} trades from bot logs")

    # On-chain weather data
    onchain_file = LOG_DIR / "weather_trades_onchain.json"
    if onchain_file.exists():
        try:
            onchain_data = json.loads(onchain_file.read_text())
            onchain_trades = []
            for d in onchain_data:
                t = Trade(
                    timestamp=d.get("timestamp", ""),
                    strategy=d.get("strategy", ""),
                    city=d.get("city", ""),
                    direction=d.get("direction", ""),
                    price=d.get("price", 0),
                    size=d.get("size", 0),
                    edge=d.get("edge", 0),
                    confidence=d.get("confidence", 0),
                    sources=d.get("sources", 1),
                    horizon=d.get("horizon", 0),
                    outcome=d.get("outcome", ""),
                    pnl=d.get("pnl", 0),
                    question=d.get("question", ""),
                    payoff=d.get("payoff", 0),
                    uncertainty=d.get("uncertainty", 0),
                )
                onchain_trades.append(t)
            if onchain_trades:
                existing_questions = {t.question for t in trades}
                for t in onchain_trades:
                    if t.question not in existing_questions:
                        trades.append(t)
                print(f"  + {len(onchain_trades)} on-chain trades loaded")
        except Exception as e:
            print(f"  Warning: could not load on-chain trades: {e}")

    return trades


def optimize_strategy(trades: list[Trade], strategy: str, max_iter: int,
                      auto_apply: bool) -> dict:
    """Run optimization for a single strategy. Returns summary dict."""
    param_ranges = STRATEGY_PARAMS.get(strategy)
    if not param_ranges:
        print(f"  No param ranges defined for {strategy}, skipping.")
        return {"strategy": strategy, "status": "no_params"}

    strategy_trades = [t for t in trades if t.strategy == strategy]
    closed = [t for t in strategy_trades if t.outcome in ("WIN", "LOSS")]

    print(f"\nLoaded {len(trades)} trades, {len(strategy_trades)} {strategy} "
          f"({len(closed)} closed)")

    if len(closed) < 10:
        print(f"Not enough closed trades ({len(closed)} < 10). Skipping {strategy}.")
        return {"strategy": strategy, "status": "insufficient_data",
                "closed": len(closed)}

    # Run optimization with unique seed per strategy
    random.seed(hash(strategy) + 42)
    experiments = run_optimization(
        trades, param_ranges,
        max_iter=max_iter, strategy=strategy
    )

    # Save
    exp_file = LOG_DIR / f"auto_optimizer_{strategy}.json"
    save_experiments(experiments, exp_file)
    save_results_tsv(experiments, strategy)

    # Report
    result = print_recommendations(experiments, param_ranges, strategy)
    improvement_pct, changes, best = result

    # Auto-apply
    applied = False
    if auto_apply:
        applied = auto_apply_params(strategy, changes, best, improvement_pct)

    return {
        "strategy": strategy,
        "status": "ok",
        "closed": len(closed),
        "improvement_pct": improvement_pct,
        "applied": applied,
        "best_score": best.score,
        "best_metrics": best.metrics,
    }


def main():
    parser = argparse.ArgumentParser(
        description="AutoOptimizer v2.0 — multi-strategy parameter optimization"
    )
    parser.add_argument(
        "--strategy", default="all",
        choices=["weather", "favorite_longshot", "abandoned_position", "all"],
        help="Strategy to optimize (default: all)"
    )
    parser.add_argument(
        "--max-iter", type=int, default=200,
        help="Maximum iterations per strategy (default: 200)"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Show results from previous optimization run"
    )
    parser.add_argument(
        "--auto-apply", action="store_true",
        help=f"Auto-apply params if improvement >{AUTO_APPLY_MIN_IMPROVEMENT}%% "
             f"and >{AUTO_APPLY_MIN_CLOSED} closed trades"
    )
    parser.add_argument(
        "--continuous", action="store_true",
        help="Karpathy mode: loop forever, re-loading trades each round. "
             "Ctrl+C to stop. Best left running overnight."
    )
    parser.add_argument(
        "--interval", type=int, default=1800,
        help="Seconds between continuous rounds (default: 1800 = 30min)"
    )
    args = parser.parse_args()

    if args.strategy == "all":
        strategies = list(STRATEGY_PARAMS.keys())
    else:
        strategies = [args.strategy]

    if args.report:
        for strategy in strategies:
            param_ranges = STRATEGY_PARAMS.get(strategy, [])
            exp_file = LOG_DIR / f"auto_optimizer_{strategy}.json"
            # Fallback to old filename
            if not exp_file.exists():
                exp_file = LOG_DIR / "auto_optimizer.json"
            if not exp_file.exists():
                print(f"No previous results for {strategy}.")
                continue
            data = json.loads(exp_file.read_text())
            experiments = [Experiment(**d) for d in data]
            print_recommendations(experiments, param_ranges, strategy)
        return

    def run_round():
        """Single optimization round."""
        trades = load_trades()
        if not trades:
            print("No trades found. Run the bot first to generate trade data.")
            return []

        strat_counts = {}
        for t in trades:
            if t.outcome in ("WIN", "LOSS"):
                strat_counts[t.strategy] = strat_counts.get(t.strategy, 0) + 1
        print(f"\nClosed trades by strategy:")
        for s, c in sorted(strat_counts.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")

        results = []
        for strategy in strategies:
            result = optimize_strategy(trades, strategy, args.max_iter, args.auto_apply)
            results.append(result)

        if len(results) > 1:
            print(f"\n{'='*70}")
            print(f"  OPTIMIZATION SUMMARY")
            print(f"{'='*70}")
            for r in results:
                status = r.get("status", "?")
                if status == "ok":
                    imp = r.get("improvement_pct", 0)
                    applied = "✅ APPLIED" if r.get("applied") else ""
                    print(f"  {r['strategy']:25s} | {r['closed']:3d} closed | "
                          f"improvement={imp:+.1f}% | score={r['best_score']:.4f} "
                          f"{applied}")
                elif status == "insufficient_data":
                    print(f"  {r['strategy']:25s} | {r.get('closed', 0):3d} closed | "
                          f"⏭ insufficient data")
                else:
                    print(f"  {r['strategy']:25s} | {status}")
        return results

    # First round
    run_round()

    # v2.2 (Karpathy): continuous mode — NEVER STOP
    if args.continuous:
        round_num = 1
        print(f"\n{'='*70}")
        print(f"  CONTINUOUS MODE — interval {args.interval}s. Ctrl+C to stop.")
        print(f"{'='*70}")
        try:
            while True:
                print(f"\n  Sleeping {args.interval}s until next round...")
                time.sleep(args.interval)
                round_num += 1
                print(f"\n{'#'*70}")
                print(f"  ROUND {round_num} — {time.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'#'*70}")
                run_round()
        except KeyboardInterrupt:
            print(f"\n  Stopped after {round_num} rounds.")


if __name__ == "__main__":
    main()
