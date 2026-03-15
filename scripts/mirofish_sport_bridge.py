"""
MiroFish Sport Prediction Bridge (v12.6)
==========================================
Updated to use CrowdSportStrategy (Delphi multi-agent simulation) instead
of duplicating market fetch / prediction logic.

Now acts as a thin CLI wrapper around strategies/crowd_sport.py.

Usage:
    python3 scripts/mirofish_sport_bridge.py              # single scan
    python3 scripts/mirofish_sport_bridge.py --daemon      # continuous (every 4h)
    python3 scripts/mirofish_sport_bridge.py --status      # show prediction history
"""

import argparse
import logging
import sys
import time

# Add parent dir to path so we can import from strategies/
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from strategies.crowd_sport import CrowdSportStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("mirofish_bridge")


def main():
    parser = argparse.ArgumentParser(description="Sport Prediction Bridge (Delphi Crowd)")
    parser.add_argument("--daemon", action="store_true", help="Run continuously (every 4h)")
    parser.add_argument("--status", action="store_true", help="Show latest predictions")
    parser.add_argument("--interval", type=float, default=4, help="Hours between scans (daemon)")
    parser.add_argument("--limit", type=int, default=10, help="Max markets to simulate")
    args = parser.parse_args()

    strategy = CrowdSportStrategy()
    strategy.MAX_MARKETS_PER_SCAN = args.limit

    if args.status:
        # Delegate to crowd_sport CLI
        from strategies.crowd_sport import RESULTS_FILE
        import json
        if RESULTS_FILE.exists():
            preds = json.loads(RESULTS_FILE.read_text())
            print(f"\n{'='*70}")
            print(f"CROWD SPORT PREDICTIONS (Delphi) — {len(preds)} total")
            print(f"{'='*70}")
            for p in preds[-10:]:
                print(
                    f"  {p['side']} edge={p['edge']:.1%} "
                    f"crowd={p['crowd_probability']:.3f} PM={p['polymarket_price']:.3f} "
                    f"${p['kelly_size']:.0f} conf={p['confidence']:.2f} "
                    f"std={p['std_dev']:.3f} | {p['question'][:50]}"
                )
        else:
            print("No predictions yet. Run a scan first.")
        return

    if args.daemon:
        logger.info(f"[CROWD-SPORT] Daemon mode — scan every {args.interval}h, limit={args.limit}")
        while True:
            try:
                signals = strategy.scan()
                if signals:
                    logger.info(f"[CROWD-SPORT] {len(signals)} signals found")
                    for s in signals:
                        logger.info(
                            f"  -> {s.side} edge={s.edge:.1%} "
                            f"crowd={s.crowd_probability:.3f} vs PM={s.polymarket_price:.3f} "
                            f"${s.kelly_size:.0f} | {s.question[:50]}"
                        )
                else:
                    logger.info("[CROWD-SPORT] No signals")
            except Exception as e:
                logger.error(f"[CROWD-SPORT] Scan error: {e}")
            time.sleep(args.interval * 3600)
    else:
        signals = strategy.scan()
        if signals:
            print(f"\n{len(signals)} signals found:")
            for s in signals:
                print(
                    f"  {s.side} edge={s.edge:.1%} crowd={s.crowd_probability:.3f} "
                    f"PM={s.polymarket_price:.3f} ${s.kelly_size:.0f} "
                    f"conf={s.confidence:.2f} | {s.question[:60]}"
                )
        else:
            print("No signals found.")


if __name__ == "__main__":
    main()
