#!/usr/bin/env python3
"""
Hyperspace Bridge — Ponte tra il bot Polymarket e la rete Hyperspace AGI.

Gira in background (o come cron) e:
1. Ogni ora: pubblica i risultati dell'AutoOptimizer su Hyperspace
2. Ogni ora: cerca scoperte peer e le logga
3. Se un peer ha risultati 10%+ migliori, flag per review (o auto-adopt)
4. Scrive un log dedicato per monitoraggio

Uso:
    python3 scripts/hyperspace_bridge.py                # run singolo
    python3 scripts/hyperspace_bridge.py --daemon       # loop continuo (ogni ora)
    python3 scripts/hyperspace_bridge.py --interval 1800  # custom interval (30min)
    python3 scripts/hyperspace_bridge.py --auto-adopt   # adotta automaticamente

Cron (alternativa al daemon):
    0 * * * * cd /root/polymarket_toolkit && python3 scripts/hyperspace_bridge.py >> logs/hyperspace_bridge.log 2>&1
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Aggiungi parent dir al path per import
TOOLKIT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(TOOLKIT_DIR))

from hyperspace_optimizer import (
    check_hyperspace_status,
    publish_results,
    discover_peer_results,
    evaluate_peer_params,
    cross_pollinate,
    sync,
    PEER_ID,
    HYPERSPACE_BASE,
    HYPERSPACE_DIR,
    PEER_ADOPTION_THRESHOLD,
)

LOG_DIR = TOOLKIT_DIR / "logs"
BRIDGE_LOG = LOG_DIR / "hyperspace_bridge.log"
BRIDGE_STATE = HYPERSPACE_DIR / "bridge_state.json"

# Strategie da sincronizzare
STRATEGIES = ["weather", "favorite_longshot", "abandoned_position"]

# Intervallo default (secondi)
DEFAULT_INTERVAL = 3600  # 1 ora


def _log(msg: str, level: str = "INFO"):
    """Log con timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {level} | hyperspace_bridge | {msg}"
    print(line)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(BRIDGE_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load_bridge_state() -> dict:
    """Carica stato persistente del bridge."""
    if BRIDGE_STATE.exists():
        try:
            return json.loads(BRIDGE_STATE.read_text())
        except Exception:
            pass
    return {
        "total_syncs": 0,
        "total_published": 0,
        "total_discoveries": 0,
        "total_adopted": 0,
        "last_sync": None,
        "last_publish": {},
        "flagged_reviews": [],
    }


def _save_bridge_state(state: dict):
    """Salva stato del bridge."""
    HYPERSPACE_DIR.mkdir(parents=True, exist_ok=True)
    with open(BRIDGE_STATE, "w") as f:
        json.dump(state, f, indent=2)


def run_bridge_cycle(auto_adopt: bool = False) -> dict:
    """
    Esegue un ciclo completo del bridge:
    1. Check connessione Hyperspace
    2. Per ogni strategia: pubblica + scopri + valuta
    3. Se scoperte migliori: flag o adotta
    4. Aggiorna stato
    """
    state = _load_bridge_state()
    cycle_start = time.time()
    cycle_results = {
        "timestamp": datetime.now().isoformat(),
        "strategies": {},
        "node_connected": False,
        "errors": [],
    }

    # 1. Verifica nodo Hyperspace
    _log("Verifico connessione Hyperspace...")
    status = check_hyperspace_status()
    cycle_results["node_connected"] = status["connected"]

    if not status["connected"]:
        _log(f"Nodo Hyperspace non raggiungibile a {HYPERSPACE_BASE}", "WARN")
        _log("Continuo comunque (salvataggio locale + GitHub snapshots)")

    # 2. Per ogni strategia
    for strategy in STRATEGIES:
        _log(f"--- Sync {strategy} ---")
        strat_result = {
            "published": False,
            "discoveries": 0,
            "evaluated": 0,
            "flagged": [],
            "adopted": None,
        }

        try:
            # Pubblica
            run = publish_results(strategy)
            if run:
                strat_result["published"] = True
                strat_result["published_score"] = run.score
                state["total_published"] = state.get("total_published", 0) + 1
                state["last_publish"][strategy] = {
                    "timestamp": datetime.now().isoformat(),
                    "score": run.score,
                    "run_number": run.run_number,
                }
                _log(f"[{strategy}] Pubblicato run #{run.run_number} score={run.score:.4f}")
            else:
                _log(f"[{strategy}] Nessun risultato da pubblicare", "WARN")

            # Scopri peer
            discoveries = discover_peer_results()
            strat_result["discoveries"] = len(discoveries)
            state["total_discoveries"] = (
                state.get("total_discoveries", 0) + len(discoveries)
            )
            if discoveries:
                _log(f"[{strategy}] {len(discoveries)} scoperte peer")

            # Valuta e cross-pollina
            adopted = cross_pollinate(strategy, auto_adopt=auto_adopt)
            if adopted:
                strat_result["adopted"] = {
                    "peer_id": adopted["peer_id"][:8],
                    "improvement": adopted["improvement_pct"],
                    "local_score": adopted["local_score"],
                }
                state["total_adopted"] = state.get("total_adopted", 0) + 1

                if adopted["improvement_pct"] >= PEER_ADOPTION_THRESHOLD * 100:
                    flag_entry = {
                        "timestamp": datetime.now().isoformat(),
                        "strategy": strategy,
                        "peer_id": adopted["peer_id"][:8],
                        "improvement_pct": adopted["improvement_pct"],
                        "local_score": adopted["local_score"],
                        "params": adopted.get("params", {}),
                        "auto_adopted": auto_adopt,
                    }
                    strat_result["flagged"].append(flag_entry)
                    state.setdefault("flagged_reviews", []).append(flag_entry)

                    if auto_adopt:
                        _log(
                            f"[{strategy}] AUTO-ADOTTATI parametri peer "
                            f"{adopted['peer_id'][:8]} "
                            f"({adopted['improvement_pct']:+.1f}%)",
                            "IMPORTANT"
                        )
                    else:
                        _log(
                            f"[{strategy}] FLAGGED: peer {adopted['peer_id'][:8]} "
                            f"ha parametri {adopted['improvement_pct']:+.1f}% migliori! "
                            f"Usa --auto-adopt per adottare automaticamente.",
                            "IMPORTANT"
                        )

        except Exception as e:
            err_msg = f"[{strategy}] Errore: {e}"
            _log(err_msg, "ERROR")
            cycle_results["errors"].append(err_msg)

        cycle_results["strategies"][strategy] = strat_result

    # 3. Aggiorna stato
    state["total_syncs"] = state.get("total_syncs", 0) + 1
    state["last_sync"] = datetime.now().isoformat()
    _save_bridge_state(state)

    elapsed = time.time() - cycle_start
    _log(f"Ciclo completato in {elapsed:.1f}s — "
         f"syncs totali: {state['total_syncs']}, "
         f"pubblicati: {state['total_published']}, "
         f"adottati: {state['total_adopted']}")

    return cycle_results


def print_bridge_status():
    """Mostra stato corrente del bridge."""
    state = _load_bridge_state()
    hs_status = check_hyperspace_status()

    print(f"\n{'='*60}")
    print(f"  HYPERSPACE BRIDGE STATUS")
    print(f"{'='*60}")
    print(f"  Peer ID:         {PEER_ID}")
    print(f"  Node:            {HYPERSPACE_BASE}")
    print(f"  Connected:       {hs_status['connected']}")
    print(f"  Total syncs:     {state.get('total_syncs', 0)}")
    print(f"  Total published: {state.get('total_published', 0)}")
    print(f"  Total discoveries: {state.get('total_discoveries', 0)}")
    print(f"  Total adopted:   {state.get('total_adopted', 0)}")
    print(f"  Last sync:       {state.get('last_sync', 'mai')}")

    flagged = state.get("flagged_reviews", [])
    if flagged:
        print(f"\n  Flagged per review ({len(flagged)}):")
        for f in flagged[-5:]:  # ultime 5
            print(f"    {f.get('timestamp', '?')} | {f.get('strategy', '?')} | "
                  f"peer {f.get('peer_id', '?')} | "
                  f"{f.get('improvement_pct', 0):+.1f}% | "
                  f"adopted: {f.get('auto_adopted', False)}")

    last_pub = state.get("last_publish", {})
    if last_pub:
        print(f"\n  Ultima pubblicazione per strategia:")
        for strat, info in last_pub.items():
            print(f"    {strat}: score={info.get('score', 0):.4f} "
                  f"run#{info.get('run_number', 0)} "
                  f"({info.get('timestamp', '?')})")

    print(f"{'='*60}\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Hyperspace Bridge — ponte tra bot e rete P2P"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Modalita' daemon: loop continuo"
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"Intervallo tra cicli in secondi (default: {DEFAULT_INTERVAL})"
    )
    parser.add_argument(
        "--auto-adopt", action="store_true",
        help="Adotta automaticamente parametri peer 10%%+ migliori"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Mostra stato corrente del bridge"
    )
    parser.add_argument(
        "--strategy", default=None,
        choices=["weather", "favorite_longshot", "abandoned_position"],
        help="Sincronizza solo questa strategia"
    )
    args = parser.parse_args()

    if args.status:
        print_bridge_status()
        return

    # Filtra strategie se specificata
    global STRATEGIES
    if args.strategy:
        STRATEGIES = [args.strategy]

    if args.daemon:
        _log(f"Bridge avviato in modalita' daemon — intervallo {args.interval}s")
        _log(f"Peer ID: {PEER_ID}")
        _log(f"Nodo: {HYPERSPACE_BASE}")
        _log(f"Strategie: {', '.join(STRATEGIES)}")
        _log(f"Auto-adopt: {args.auto_adopt}")

        cycle = 0
        try:
            while True:
                cycle += 1
                _log(f"\n--- CICLO #{cycle} ---")
                try:
                    run_bridge_cycle(auto_adopt=args.auto_adopt)
                except Exception as e:
                    _log(f"Errore ciclo #{cycle}: {e}", "ERROR")

                _log(f"Prossimo ciclo tra {args.interval}s...")
                time.sleep(args.interval)

        except KeyboardInterrupt:
            _log(f"Bridge fermato dopo {cycle} cicli")
    else:
        # Singolo ciclo
        _log("Bridge: esecuzione singola")
        run_bridge_cycle(auto_adopt=args.auto_adopt)


if __name__ == "__main__":
    main()
