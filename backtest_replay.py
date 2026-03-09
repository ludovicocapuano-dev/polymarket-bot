#!/usr/bin/env python3
"""
Backtest Replay Framework — Simula trade passati con parametri diversi.

Uso:
    python3 backtest_replay.py                    # replay con parametri attuali
    python3 backtest_replay.py --min-edge 0.10    # testa min_edge diverso
    python3 backtest_replay.py --min-confidence 0.60
    python3 backtest_replay.py --min-payoff 0.30
    python3 backtest_replay.py --max-price 0.75
    python3 backtest_replay.py --min-sources 2
    python3 backtest_replay.py --compare          # confronta attuale vs proposto

Legge i trade dai log e simula cosa sarebbe successo con filtri diversi.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOG_DIR = Path(__file__).parent / "logs"


@dataclass
class Trade:
    timestamp: str = ""
    strategy: str = ""
    city: str = ""
    direction: str = ""  # BUY_NO / BUY_YES
    price: float = 0.0
    size: float = 0.0
    edge: float = 0.0
    confidence: float = 0.0
    sources: int = 1
    horizon: int = 0  # days ahead
    outcome: str = ""  # WIN / LOSS / OPEN
    pnl: float = 0.0
    question: str = ""
    payoff: float = 0.0
    uncertainty: float = 0.0


def parse_trades_from_logs(log_files: list[Path]) -> list[Trade]:
    """Estrai trade dai log del bot usando grep per velocita'.

    Formato log reale:
      APERTO: 2026-02-20 21:42:18,105 | INFO | utils.risk_manager | [weather] APERTO BUY_NO $25.00 @0.9125 edge=0.0771
      VINTO:  2026-02-22 08:18:09,229 | INFO | utils.risk_manager | [weather] VINTO PnL=$+7.26 | Giorn=...
      PERSO:  2026-02-20 23:04:31,558 | INFO | utils.risk_manager | [weather] PERSO PnL=$-24.97 | Giorn=...
    """
    import subprocess

    log_dir = log_files[0].parent if log_files else LOG_DIR
    log_pattern = str(log_dir / "bot_*.log")

    # Use grep for speed on large log files
    open_trades: dict[str, list] = {}
    outcomes: dict[str, list] = {}

    aperto_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?"
        r"\[(\w+)\] APERTO (BUY_(?:NO|YES)) \$([\d.]+) @([\d.]+) edge=([\d.]+)"
    )
    chiuso_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?"
        r"\[(\w+)\] (VINTO|PERSO) PnL=\$([\+\-]?[\d.]+)"
    )

    # Grep APERTO lines
    try:
        result = subprocess.run(
            ["grep", "-h", "APERTO BUY_", *[str(f) for f in log_files]],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.split("\n"):
            m = aperto_re.search(line)
            if m:
                ts, strategy, direction, size, price, edge = (
                    m.group(1), m.group(2), m.group(3),
                    float(m.group(4)), float(m.group(5)), float(m.group(6)),
                )
                t = Trade(
                    timestamp=ts,
                    strategy=strategy,
                    direction=direction,
                    size=size,
                    price=price,
                    edge=edge,
                    payoff=(1.0 - price) / price if price > 0 else 0,
                )
                open_trades.setdefault(strategy, []).append(t)
    except Exception:
        pass

    # Grep VINTO/PERSO lines
    try:
        result = subprocess.run(
            ["grep", "-hE", "VINTO PnL=|PERSO PnL=", *[str(f) for f in log_files]],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.split("\n"):
            m = chiuso_re.search(line)
            if m:
                ts, strategy, result_str, pnl_str = (
                    m.group(1), m.group(2), m.group(3), m.group(4),
                )
                outcome = "WIN" if result_str == "VINTO" else "LOSS"
                pnl = float(pnl_str)
                outcomes.setdefault(strategy, []).append((ts, outcome, pnl))
    except Exception:
        pass

    # Match outcomes to trades in order (FIFO per strategy)
    trades = []
    for strategy in set(list(open_trades.keys()) + list(outcomes.keys())):
        strat_opens = open_trades.get(strategy, [])
        strat_outcomes = outcomes.get(strategy, [])

        for i, t in enumerate(strat_opens):
            if i < len(strat_outcomes):
                _, outcome, pnl = strat_outcomes[i]
                t.outcome = outcome
                t.pnl = pnl
            else:
                t.outcome = "OPEN"
            trades.append(t)

    return trades


def parse_trades_json() -> list[Trade]:
    """Leggi trade da trades.json se esiste."""
    trades_file = LOG_DIR / "trades.json"
    if not trades_file.exists():
        return []

    try:
        data = json.loads(trades_file.read_text())
    except Exception:
        return []

    trades = []
    for d in data:
        t = Trade(
            timestamp=d.get("timestamp", ""),
            strategy=d.get("strategy", ""),
            city=d.get("city", ""),
            direction=d.get("direction", ""),
            price=d.get("entry_price", d.get("price", 0)),
            size=d.get("size", d.get("amount", 0)),
            edge=d.get("edge", 0),
            confidence=d.get("confidence", 0),
            sources=d.get("sources", d.get("n_sources", 1)),
            horizon=d.get("horizon", d.get("days_ahead", 0)),
            outcome=d.get("outcome", "") or d.get("result", ""),
            pnl=d.get("pnl", 0),
            question=d.get("question", ""),
            payoff=d.get("payoff", 0),
            uncertainty=d.get("uncertainty", 0),
        )
        trades.append(t)
    return trades


@dataclass
class FilterParams:
    min_edge: float = 0.04
    min_confidence: float = 0.55
    min_payoff: float = 0.25
    max_price: float = 0.85
    min_sources: int = 1
    min_sources_high_price: int = 2  # required sources if price > 0.65
    high_price_threshold: float = 0.65


def apply_filters(trades: list[Trade], params: FilterParams) -> tuple[list[Trade], list[Trade]]:
    """Applica filtri e ritorna (passano, bloccati)."""
    passed = []
    blocked = []

    for t in trades:
        if t.strategy != "weather":
            passed.append(t)
            continue

        block_reason = None

        # Min edge (per orizzonte)
        if t.horizon == 0 and t.edge > 0 and t.edge < 0.02:
            block_reason = f"edge {t.edge:.3f} < 0.02 (same-day)"
        elif t.horizon == 1 and t.edge > 0 and t.edge < params.min_edge:
            block_reason = f"edge {t.edge:.3f} < {params.min_edge} (+1d)"
        elif t.horizon >= 2 and t.edge > 0 and t.edge < 0.12:
            block_reason = f"edge {t.edge:.3f} < 0.12 (+2d)"

        # Confidence
        if not block_reason and t.confidence > 0 and t.confidence < params.min_confidence:
            block_reason = f"confidence {t.confidence:.2f} < {params.min_confidence}"

        # Max price
        if not block_reason and t.price > 0 and t.price > params.max_price:
            block_reason = f"price {t.price:.3f} > {params.max_price}"

        # Multi-source per high price
        if not block_reason and t.price > params.high_price_threshold and t.sources < params.min_sources_high_price:
            block_reason = f"single-source at price {t.price:.3f}"

        # Payoff
        if not block_reason and t.payoff > 0 and t.payoff < params.min_payoff:
            block_reason = f"payoff {t.payoff:.3f} < {params.min_payoff}"

        if block_reason:
            t.question = block_reason  # riusa campo per annotare
            blocked.append(t)
        else:
            passed.append(t)

    return passed, blocked


def calc_metrics(trades: list[Trade], strategy: str = "weather") -> dict:
    """Calcola metriche da una lista di trade per una strategia."""
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


def print_report(label: str, trades: list[Trade], blocked: list[Trade]):
    """Stampa report formattato."""
    m = calc_metrics(trades)
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Trade weather:   {m['total']}")
    print(f"  Chiusi:          {m['closed']} ({m['wins']}W / {m['losses']}L)")
    print(f"  Win Rate:        {m['wr']:.1f}%")
    print(f"  PnL:             ${m['pnl']:+.2f}")
    print(f"  Profit Factor:   {m['profit_factor']:.2f}")
    print(f"  Avg PnL/trade:   ${m['avg_pnl']:+.2f}")

    if blocked:
        blocked_wins = sum(1 for t in blocked if t.outcome == "WIN")
        blocked_losses = sum(1 for t in blocked if t.outcome == "LOSS")
        blocked_pnl = sum(t.pnl for t in blocked)
        print(f"\n  Trade BLOCCATI:   {len(blocked)}")
        print(f"    di cui WIN:    {blocked_wins}")
        print(f"    di cui LOSS:   {blocked_losses}")
        print(f"    PnL bloccato:  ${blocked_pnl:+.2f}")
        if blocked:
            print(f"\n  Motivi blocco:")
            reasons = {}
            for t in blocked:
                r = t.question.split(":")[0] if t.question else "unknown"
                reasons[r] = reasons.get(r, 0) + 1
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
                print(f"    {reason}: {count}")


def main():
    parser = argparse.ArgumentParser(description="Backtest replay con filtri diversi")
    parser.add_argument("--min-edge", type=float, default=0.08, help="Min edge per +1d (default: 0.08)")
    parser.add_argument("--min-confidence", type=float, default=0.55, help="Min confidence (default: 0.55)")
    parser.add_argument("--min-payoff", type=float, default=0.25, help="Min payoff ratio (default: 0.25)")
    parser.add_argument("--max-price", type=float, default=0.85, help="Max entry price (default: 0.85)")
    parser.add_argument("--min-sources", type=int, default=2, help="Min sources per high price (default: 2)")
    parser.add_argument("--compare", action="store_true", help="Confronta parametri attuali vs proposti")
    args = parser.parse_args()

    # Raccogli trade
    log_files = sorted(LOG_DIR.glob("bot_*.log"))
    trades_log = parse_trades_from_logs(log_files)
    trades_json = parse_trades_json()

    # Preferisci trades.json se ha dati, altrimenti log
    trades = trades_json if trades_json else trades_log

    if not trades:
        print("Nessun trade trovato in logs/ o trades.json")
        sys.exit(1)

    weather_trades = [t for t in trades if t.strategy == "weather"]
    print(f"Trovati {len(trades)} trade totali, {len(weather_trades)} weather")

    if args.compare:
        # Parametri "vecchi" (pre-ottimizzazione)
        old_params = FilterParams(
            min_edge=0.04,
            min_confidence=0.45,
            min_payoff=0.25,
            max_price=0.85,
            min_sources=1,
            min_sources_high_price=1,
        )
        old_passed, old_blocked = apply_filters(list(trades), old_params)
        print_report("PARAMETRI VECCHI (pre-ottimizzazione)", old_passed, old_blocked)

        # Parametri nuovi
        new_params = FilterParams(
            min_edge=args.min_edge,
            min_confidence=args.min_confidence,
            min_payoff=args.min_payoff,
            max_price=args.max_price,
            min_sources_high_price=args.min_sources,
        )
        new_passed, new_blocked = apply_filters(list(trades), new_params)
        print_report("PARAMETRI NUOVI (proposti)", new_passed, new_blocked)

        # Delta
        old_m = calc_metrics(old_passed)
        new_m = calc_metrics(new_passed)
        print(f"\n{'='*60}")
        print(f"  DELTA")
        print(f"{'='*60}")
        print(f"  Trade:         {new_m['total'] - old_m['total']:+d}")
        print(f"  Win Rate:      {new_m['wr'] - old_m['wr']:+.1f}%")
        print(f"  PnL:           ${new_m['pnl'] - old_m['pnl']:+.2f}")
        print(f"  Profit Factor: {new_m['profit_factor'] - old_m['profit_factor']:+.2f}")
    else:
        params = FilterParams(
            min_edge=args.min_edge,
            min_confidence=args.min_confidence,
            min_payoff=args.min_payoff,
            max_price=args.max_price,
            min_sources_high_price=args.min_sources,
        )
        passed, blocked = apply_filters(trades, params)
        print_report(f"REPLAY (edge={args.min_edge}, conf={args.min_confidence})", passed, blocked)


if __name__ == "__main__":
    main()
