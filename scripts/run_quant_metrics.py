#!/usr/bin/env python3
"""Run PSR/DSR/binHR evaluation on all strategies."""
import json, sys
sys.path.insert(0, '/root/polymarket_toolkit')
from monitoring.quant_metrics import evaluate_all_strategies

trades_file = 'logs/trades.json'
try:
    with open(trades_file) as f:
        trades = json.load(f)
except Exception:
    print("[QUANT] No trades.json found")
    exit(0)

by_strat = {}
for t in trades:
    if t.get('result') in ('WIN', 'LOSS') and t.get('pnl') is not None:
        by_strat.setdefault(t['strategy'], []).append(t['pnl'])

if not by_strat:
    print("[QUANT] No closed trades with PnL data yet")
    exit(0)

reports = evaluate_all_strategies(by_strat, n_tested=13)
for name, r in reports.items():
    status = "OK" if r.is_structurally_viable else "AT RISK"
    sig = "SIGNIFICANT" if r.is_significantly_profitable else "NOT SIG"
    print(f"[QUANT] {name}: WR={r.win_rate:.1%} BE={r.breakeven_precision:.1%} "
          f"PSR={r.psr:.3f} P(fail)={r.prob_failure:.3f} [{sig}] [{status}]")
