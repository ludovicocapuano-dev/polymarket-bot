"""
AutoContext ↔ Polymarket Bot Bridge
====================================
Runs AutoContext generation loop on polymarket_trading scenario,
then applies the best parameters to the bot's hot-reload config.

Usage:
    python3 scripts/autocontext_bridge.py              # single run (3 generations)
    python3 scripts/autocontext_bridge.py --daemon      # continuous (every 6h)
    python3 scripts/autocontext_bridge.py --status      # show latest results
    python3 scripts/autocontext_bridge.py --apply       # apply best params to bot
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("autocontext_bridge")

AUTOCONTEXT_DIR = Path("/root/autocontext/autocontext")
BOT_DIR = Path("/root/polymarket_toolkit")
PLAYBOOK_FILE = Path("/root/autocontext/knowledge/polymarket_trading/playbook.md")
APPLIED_FILE = BOT_DIR / "logs" / "autocontext_applied.json"
BRIDGE_LOG = BOT_DIR / "logs" / "autocontext_bridge.log"

# Environment for autoctx commands
AUTOCTX_ENV = {
    **os.environ,
    "PATH": f"/root/.local/bin:{os.environ.get('PATH', '')}",
}


def run_generation(gens: int = 3, run_id: str | None = None) -> bool:
    """Run AutoContext generation loop."""
    if not run_id:
        run_id = f"polymarket_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    logger.info(f"[AUTOCONTEXT] Starting {gens} generations (run_id={run_id})")

    cmd = [
        "/root/.local/bin/uv", "run", "autoctx", "run",
        "--scenario", "polymarket_trading",
        "--gens", str(gens),
        "--run-id", run_id,
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(AUTOCONTEXT_DIR),
            env=AUTOCTX_ENV,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
        )
        if result.returncode == 0:
            logger.info(f"[AUTOCONTEXT] Generation complete: {run_id}")
            return True
        else:
            logger.error(f"[AUTOCONTEXT] Failed: {result.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("[AUTOCONTEXT] Timeout after 600s")
        return False
    except Exception as e:
        logger.error(f"[AUTOCONTEXT] Error: {e}")
        return False


def read_playbook() -> str:
    """Read the current playbook."""
    if PLAYBOOK_FILE.exists():
        return PLAYBOOK_FILE.read_text()
    return ""


def extract_params_from_playbook(playbook: str) -> dict | None:
    """Extract parameter recommendations from playbook text."""
    # Look for JSON blocks in the playbook
    import re
    json_pattern = re.compile(r'\{[^{}]*"max_bet_no"[^{}]*\}', re.DOTALL)
    match = json_pattern.search(playbook)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def apply_params(params: dict, dry_run: bool = False) -> bool:
    """Apply AutoContext-suggested params to the bot's hot-reload config."""
    current_file = BOT_DIR / "logs" / "auto_optimizer_applied_weather.json"

    # Load current params
    current = {}
    if current_file.exists():
        try:
            current = json.loads(current_file.read_text())
        except Exception:
            pass

    # Merge — only update params that AutoContext suggests
    merged = {**current, **params}

    if dry_run:
        logger.info(f"[DRY-RUN] Would apply: {json.dumps(merged, indent=2)}")
        return True

    # Save
    current_file.write_text(json.dumps(merged, indent=2))

    # Log application
    applied = {
        "timestamp": datetime.now().isoformat(),
        "source": "autocontext",
        "params": merged,
        "previous": current,
    }
    APPLIED_FILE.write_text(json.dumps(applied, indent=2))
    logger.info(f"[AUTOCONTEXT] Applied params to bot: {list(params.keys())}")
    return True


def show_status():
    """Show latest AutoContext results."""
    print("=" * 60)
    print("AUTOCONTEXT BRIDGE STATUS")
    print("=" * 60)

    # Show playbook excerpt
    playbook = read_playbook()
    if playbook:
        lines = playbook.split("\n")
        print(f"\nPlaybook: {len(lines)} lines")
        # Show last PLAYBOOK section
        in_section = False
        for line in lines:
            if "PLAYBOOK_START" in line:
                in_section = True
                continue
            if "PLAYBOOK_END" in line:
                in_section = False
                continue
            if in_section:
                print(f"  {line}")
    else:
        print("\nNo playbook found")

    # Show last applied
    if APPLIED_FILE.exists():
        applied = json.loads(APPLIED_FILE.read_text())
        print(f"\nLast applied: {applied.get('timestamp', '?')}")
        print(f"Params: {json.dumps(applied.get('params', {}), indent=2)}")
    else:
        print("\nNo params applied yet")
    print("=" * 60)


def daemon_loop(interval_hours: float = 6.0, gens: int = 3):
    """Run continuously."""
    logger.info(f"[DAEMON] Starting AutoContext bridge (every {interval_hours}h, {gens} gens)")
    while True:
        try:
            success = run_generation(gens=gens)
            if success:
                playbook = read_playbook()
                params = extract_params_from_playbook(playbook)
                if params:
                    logger.info(f"[DAEMON] Extracted params: {list(params.keys())}")
                    # Don't auto-apply — just log for review
                    logger.info(f"[DAEMON] Suggested params: {json.dumps(params)}")
                else:
                    logger.info("[DAEMON] No param changes extracted from playbook")
        except Exception as e:
            logger.error(f"[DAEMON] Error: {e}")

        logger.info(f"[DAEMON] Sleeping {interval_hours}h")
        time.sleep(interval_hours * 3600)


def main():
    parser = argparse.ArgumentParser(description="AutoContext ↔ Polymarket Bot Bridge")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=float, default=6.0, help="Hours between runs (daemon mode)")
    parser.add_argument("--gens", type=int, default=3, help="Generations per run")
    parser.add_argument("--status", action="store_true", help="Show status")
    parser.add_argument("--apply", action="store_true", help="Apply latest playbook params")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually apply")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.apply:
        playbook = read_playbook()
        params = extract_params_from_playbook(playbook)
        if params:
            apply_params(params, dry_run=args.dry_run)
        else:
            print("No params found in playbook to apply")
        return

    if args.daemon:
        daemon_loop(interval_hours=args.interval, gens=args.gens)
    else:
        run_generation(gens=args.gens)


if __name__ == "__main__":
    main()
