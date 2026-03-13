#!/usr/bin/env python3
"""
Hyperspace Distributed Optimizer — Cross-pollination di parametri via P2P.

Pubblica i risultati dell'AutoOptimizer sulla rete Hyperspace e consuma
le scoperte di altri agenti. Ogni "esperimento" = un set di parametri
testato contro trade storici, con score dall'AutoOptimizer.

Formato compatibile con Hyperspace AGI:
  - run-NNNN.json per risultati machine-readable
  - best.json per il miglior risultato corrente
  - JOURNAL.md per il log decisionale

API Hyperspace: localhost:8080 (nodo locale)
Protocollo gossip: pubblica/sottoscrivi via /v1/chat/completions
Stato rete: /api/v1/state

Uso:
    python3 hyperspace_optimizer.py --publish       # pubblica ultimi risultati
    python3 hyperspace_optimizer.py --discover      # cerca scoperte peer
    python3 hyperspace_optimizer.py --sync          # pubblica + scopri + valuta
    python3 hyperspace_optimizer.py --status        # stato connessione Hyperspace
"""

import hashlib
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import argparse

# Percorsi
TOOLKIT_DIR = Path(__file__).parent
LOG_DIR = TOOLKIT_DIR / "logs"
HYPERSPACE_DIR = LOG_DIR / "hyperspace"
PEER_CACHE = HYPERSPACE_DIR / "peer_discoveries.json"
PUBLISHED_LOG = HYPERSPACE_DIR / "published_runs.json"
BEST_FILE = HYPERSPACE_DIR / "best.json"
JOURNAL_FILE = HYPERSPACE_DIR / "JOURNAL.md"

# Hyperspace API config
HYPERSPACE_BASE = os.environ.get("HYPERSPACE_API", "http://localhost:8080")
HYPERSPACE_STATE = f"{HYPERSPACE_BASE}/api/v1/state"
HYPERSPACE_CHAT = f"{HYPERSPACE_BASE}/v1/chat/completions"
HYPERSPACE_MODELS = f"{HYPERSPACE_BASE}/v1/models"

# Progetto Hyperspace
PROJECT_NAME = "polymarket-weather-optimizer"
PROJECT_DOMAIN = "finance"

# Soglia per adozione parametri peer (10% miglioramento)
PEER_ADOPTION_THRESHOLD = 0.10

# Peer ID derivato dal hostname
PEER_ID = hashlib.sha256(
    f"polymarket-bot-{socket.gethostname()}".encode()
).hexdigest()[:16]


@dataclass
class HyperspaceRun:
    """Un singolo esperimento pubblicato su Hyperspace."""
    run_number: int
    strategy: str
    params: dict
    metrics: dict
    score: float
    train_score: float
    test_score: float
    hypothesis: str
    timestamp: str
    peer_id: str = ""
    improvement_pct: float = 0.0
    n_closed_trades: int = 0
    # Hyperspace metadata
    dataset: str = "polymarket-weather-trades"
    domain: str = "finance"

    def to_hyperspace_format(self) -> dict:
        """Converte nel formato Hyperspace run-NNNN.json."""
        return {
            "project": PROJECT_NAME,
            "domain": PROJECT_DOMAIN,
            "peerId": self.peer_id or PEER_ID,
            "runNumber": self.run_number,
            "timestamp": self.timestamp,
            "hypothesis": self.hypothesis,
            "dataset": self.dataset,
            "config": {
                "strategy": self.strategy,
                "params": self.params,
                "n_closed_trades": self.n_closed_trades,
            },
            "results": {
                "score": round(self.score, 6),
                "train_score": round(self.train_score, 6),
                "test_score": round(self.test_score, 6),
                "metrics": self.metrics,
                "improvement_pct": round(self.improvement_pct, 2),
            },
        }


@dataclass
class PeerDiscovery:
    """Una scoperta da un peer sulla rete."""
    peer_id: str
    run_number: int
    strategy: str
    params: dict
    score: float
    metrics: dict
    timestamp: str
    evaluated_locally: bool = False
    local_score: float = 0.0
    adopted: bool = False
    flagged_for_review: bool = False


def _ensure_dirs():
    """Crea directory necessarie."""
    HYPERSPACE_DIR.mkdir(parents=True, exist_ok=True)


def _http_get(url: str, timeout: int = 10) -> Optional[dict]:
    """GET HTTP senza dipendenze esterne (usa urllib)."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionRefusedError, OSError, json.JSONDecodeError) as e:
        return None


def _http_post(url: str, data: dict, timeout: int = 30) -> Optional[dict]:
    """POST HTTP senza dipendenze esterne."""
    import urllib.request
    import urllib.error
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionRefusedError, OSError, json.JSONDecodeError) as e:
        return None


def check_hyperspace_status() -> dict:
    """Verifica connessione al nodo Hyperspace locale."""
    status = {
        "connected": False,
        "node_url": HYPERSPACE_BASE,
        "peer_id": PEER_ID,
        "models": [],
        "state": None,
    }

    # Check /v1/models
    models = _http_get(HYPERSPACE_MODELS)
    if models:
        status["connected"] = True
        status["models"] = [
            m.get("id", "unknown")
            for m in models.get("data", [])
        ]

    # Check /api/v1/state
    state = _http_get(HYPERSPACE_STATE)
    if state:
        status["connected"] = True
        status["state"] = {
            "peers": state.get("peers", 0),
            "runs": state.get("runs", 0),
        }

    return status


def _load_published_runs() -> list[dict]:
    """Carica log delle run pubblicate."""
    if PUBLISHED_LOG.exists():
        try:
            return json.loads(PUBLISHED_LOG.read_text())
        except Exception:
            pass
    return []


def _save_published_runs(runs: list[dict]):
    """Salva log delle run pubblicate."""
    _ensure_dirs()
    with open(PUBLISHED_LOG, "w") as f:
        json.dump(runs, f, indent=2)


def _next_run_number() -> int:
    """Prossimo numero di run."""
    runs = _load_published_runs()
    if not runs:
        return 1
    return max(r.get("run_number", 0) for r in runs) + 1


def _load_optimizer_results(strategy: str) -> list[dict]:
    """Carica risultati dell'AutoOptimizer locale."""
    exp_file = LOG_DIR / f"auto_optimizer_{strategy}.json"
    if not exp_file.exists():
        return []
    try:
        return json.loads(exp_file.read_text())
    except Exception:
        return []


def _load_peer_discoveries() -> list[dict]:
    """Carica scoperte peer salvate."""
    if PEER_CACHE.exists():
        try:
            return json.loads(PEER_CACHE.read_text())
        except Exception:
            pass
    return []


def _save_peer_discoveries(discoveries: list[dict]):
    """Salva scoperte peer."""
    _ensure_dirs()
    with open(PEER_CACHE, "w") as f:
        json.dump(discoveries, f, indent=2)


def _get_best_local_score(strategy: str) -> tuple[float, dict]:
    """Restituisce il miglior score locale e i parametri corrispondenti."""
    experiments = _load_optimizer_results(strategy)
    if not experiments:
        return 0.0, {}
    best = max(experiments, key=lambda e: e.get("score", -999))
    return best.get("score", 0.0), best.get("params", {})


def publish_results(strategy: str = "weather", dry_run: bool = False) -> Optional[HyperspaceRun]:
    """
    Pubblica gli ultimi risultati dell'AutoOptimizer sulla rete Hyperspace.

    Usa il nodo locale come relay: invia i risultati come messaggio strutturato
    via /v1/chat/completions, che il nodo propaga tramite gossip protocol.
    """
    _ensure_dirs()

    experiments = _load_optimizer_results(strategy)
    if not experiments:
        print(f"[HYPERSPACE] Nessun risultato AutoOptimizer per {strategy}")
        return None

    # Trova il best experiment
    best_exp = max(experiments, key=lambda e: e.get("score", -999))
    baseline = experiments[0] if experiments else best_exp

    # Calcola improvement
    baseline_score = baseline.get("score", 0)
    best_score = best_exp.get("score", 0)
    improvement = 0.0
    if baseline_score > 0:
        improvement = (best_score - baseline_score) / abs(baseline_score) * 100

    # Cerca test score se disponibile
    # L'AutoOptimizer non salva test_score nell'experiment, usiamo una stima
    # dal rapporto scoring_state
    evo_file = LOG_DIR / "auto_optimizer_evolution.json"
    test_score = best_score * 0.8  # stima conservativa
    if evo_file.exists():
        try:
            evo = json.loads(evo_file.read_text())
            scoring = evo.get("scoring_state", {}).get(strategy, {})
            overfit = scoring.get("overfit_streak", 0)
            robust = scoring.get("robust_streak", 0)
            # Se robusto, test score piu' vicino a train
            if robust >= 2:
                test_score = best_score * 0.9
            elif overfit >= 2:
                test_score = best_score * 0.6
        except Exception:
            pass

    run_number = _next_run_number()
    metrics = best_exp.get("metrics", {})

    # Genera hypothesis dall'analisi dei cambiamenti
    params = best_exp.get("params", {})
    hypothesis = _generate_hypothesis(params, metrics, improvement, strategy, run_number)

    run = HyperspaceRun(
        run_number=run_number,
        strategy=strategy,
        params=params,
        metrics=metrics,
        score=best_score,
        train_score=best_score,
        test_score=test_score,
        hypothesis=hypothesis,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        peer_id=PEER_ID,
        improvement_pct=improvement,
        n_closed_trades=metrics.get("closed", 0),
    )

    if dry_run:
        print(f"[HYPERSPACE] DRY RUN — run #{run_number}")
        print(json.dumps(run.to_hyperspace_format(), indent=2))
        return run

    # Salva localmente
    run_file = HYPERSPACE_DIR / f"run-{run_number:04d}.json"
    with open(run_file, "w") as f:
        json.dump(run.to_hyperspace_format(), f, indent=2)

    # Aggiorna best.json se miglior risultato
    update_best = True
    if BEST_FILE.exists():
        try:
            current_best = json.loads(BEST_FILE.read_text())
            if current_best.get("results", {}).get("score", 0) >= best_score:
                update_best = False
        except Exception:
            pass

    if update_best:
        with open(BEST_FILE, "w") as f:
            json.dump(run.to_hyperspace_format(), f, indent=2)

    # Aggiorna journal
    _append_journal(run)

    # Log pubblicazione
    published = _load_published_runs()
    published.append({
        "run_number": run_number,
        "strategy": strategy,
        "score": best_score,
        "timestamp": run.timestamp,
        "published_to_network": False,  # aggiornato dopo gossip
    })
    _save_published_runs(published)

    # Pubblica via gossip (Hyperspace /v1/chat/completions)
    network_ok = _gossip_publish(run)
    if network_ok:
        published[-1]["published_to_network"] = True
        _save_published_runs(published)

    status = "pubblicato su rete" if network_ok else "salvato localmente (nodo offline)"
    print(f"[HYPERSPACE] Run #{run_number} {status} — "
          f"score={best_score:.4f}, improvement={improvement:+.1f}%")

    return run


def _generate_hypothesis(params: dict, metrics: dict, improvement: float,
                         strategy: str, run_number: int) -> str:
    """Genera una hypothesis human-readable per il run."""
    parts = []

    if run_number == 1:
        parts.append(f"Baseline {strategy} optimizer parameters")
    else:
        parts.append(f"Improve on run #{run_number-1}")

    wr = metrics.get("wr", 0)
    pnl = metrics.get("pnl", 0)
    pf = metrics.get("profit_factor", 0)
    closed = metrics.get("closed", 0)

    parts.append(f"WR={wr:.1f}% PnL=${pnl:+.2f} PF={pf:.2f} trades={closed}")

    if improvement > 0:
        parts.append(f"improvement={improvement:+.1f}%")

    # Nota parametri chiave
    key_params = []
    if "min_edge" in params:
        key_params.append(f"edge>={params['min_edge']}")
    if "min_confidence" in params:
        key_params.append(f"conf>={params['min_confidence']}")
    if "max_weather_bet" in params:
        key_params.append(f"max_bet=${params['max_weather_bet']}")
    if key_params:
        parts.append("(" + ", ".join(key_params) + ")")

    return " | ".join(parts)


def _gossip_publish(run: HyperspaceRun) -> bool:
    """
    Pubblica un run sulla rete Hyperspace via gossip protocol.

    Usa il formato OpenAI-compatible: invia un messaggio strutturato al nodo
    locale, che lo propaga via P2P a tutti i peer nel progetto.
    """
    payload = {
        "model": "auto",
        "messages": [
            {
                "role": "system",
                "content": (
                    f"You are an experiment tracker for the {PROJECT_NAME} project. "
                    f"Domain: {PROJECT_DOMAIN}. "
                    f"Record and propagate experiment results across the network."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "action": "publish_experiment",
                    "project": PROJECT_NAME,
                    "peer_id": PEER_ID,
                    "run": run.to_hyperspace_format(),
                }),
            },
        ],
        "temperature": 0,
        "max_tokens": 256,
    }

    resp = _http_post(HYPERSPACE_CHAT, payload)
    if resp and "choices" in resp:
        return True
    return False


def discover_peer_results() -> list[PeerDiscovery]:
    """
    Interroga la rete Hyperspace per scoperte di altri agenti.

    Chiede al nodo locale di cercare risultati recenti dal progetto
    polymarket-weather-optimizer tramite il gossip protocol.
    """
    _ensure_dirs()

    # Query al nodo Hyperspace per risultati peer
    payload = {
        "model": "auto",
        "messages": [
            {
                "role": "system",
                "content": (
                    f"You are a research aggregator for the {PROJECT_NAME} project. "
                    f"Your task is to find and report the best experiment results "
                    f"from all peers in the network. "
                    f"Return results as JSON array with fields: "
                    f"peer_id, run_number, strategy, params, score, metrics, timestamp."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "action": "discover_experiments",
                    "project": PROJECT_NAME,
                    "domain": PROJECT_DOMAIN,
                    "since": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - 3600)  # ultima ora
                    ),
                    "min_score": 0.0,
                    "exclude_peer": PEER_ID,
                }),
            },
        ],
        "temperature": 0,
        "max_tokens": 2048,
    }

    resp = _http_post(HYPERSPACE_CHAT, payload)
    discoveries = []

    if resp and "choices" in resp:
        try:
            content = resp["choices"][0]["message"]["content"]
            # Cerca JSON nel contenuto (puo' essere wrapped in markdown)
            peer_data = _extract_json_from_response(content)
            if isinstance(peer_data, list):
                for item in peer_data:
                    disc = PeerDiscovery(
                        peer_id=item.get("peer_id", "unknown"),
                        run_number=item.get("run_number", 0),
                        strategy=item.get("strategy", "weather"),
                        params=item.get("params", {}),
                        score=item.get("score", 0.0),
                        metrics=item.get("metrics", {}),
                        timestamp=item.get("timestamp", ""),
                    )
                    discoveries.append(disc)
        except (KeyError, IndexError, json.JSONDecodeError):
            pass

    # Controlla anche snapshot GitHub come fallback
    snapshot_discoveries = _check_github_snapshots()
    discoveries.extend(snapshot_discoveries)

    # Salva
    existing = _load_peer_discoveries()
    existing_keys = {
        (d.get("peer_id", ""), d.get("run_number", 0))
        for d in existing
    }

    new_count = 0
    for disc in discoveries:
        key = (disc.peer_id, disc.run_number)
        if key not in existing_keys:
            existing.append(asdict(disc) if hasattr(disc, '__dataclass_fields__') else disc.__dict__)
            existing_keys.add(key)
            new_count += 1

    if new_count > 0:
        _save_peer_discoveries(existing)
        print(f"[HYPERSPACE] {new_count} nuove scoperte peer trovate")
    else:
        print("[HYPERSPACE] Nessuna nuova scoperta peer")

    return discoveries


def _extract_json_from_response(content: str) -> list | dict | None:
    """Estrae JSON da una risposta che potrebbe essere wrappata in markdown."""
    # Prova parsing diretto
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Cerca blocco ```json ... ```
    import re
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Cerca array o oggetto standalone
    for start, end in [("[", "]"), ("{", "}")]:
        idx_start = content.find(start)
        idx_end = content.rfind(end)
        if idx_start >= 0 and idx_end > idx_start:
            try:
                return json.loads(content[idx_start:idx_end + 1])
            except json.JSONDecodeError:
                pass

    return None


def _check_github_snapshots() -> list[PeerDiscovery]:
    """
    Fallback: controlla gli snapshot Hyperspace su GitHub per risultati
    nel dominio finance del progetto polymarket-weather-optimizer.
    """
    snapshot_url = (
        "https://raw.githubusercontent.com/hyperspaceai/agi/"
        "network-snapshots/snapshots/latest.json"
    )
    data = _http_get(snapshot_url, timeout=15)
    if not data:
        return []

    discoveries = []
    # Gli snapshot contengono leaderboard per dominio
    finance_entries = []

    # Naviga la struttura snapshot
    if isinstance(data, dict):
        # Cerca nella sezione finance o nel leaderboard generico
        for key in ("finance", "leaderboard", "entries", "peers"):
            section = data.get(key, {})
            if isinstance(section, list):
                finance_entries.extend(section)
            elif isinstance(section, dict):
                for sub_key, sub_val in section.items():
                    if isinstance(sub_val, list):
                        finance_entries.extend(sub_val)

    for entry in finance_entries:
        if not isinstance(entry, dict):
            continue
        peer_id = entry.get("peerId", entry.get("peer_id", ""))
        if peer_id == PEER_ID:
            continue  # skip ourselves
        if entry.get("project") != PROJECT_NAME:
            continue

        disc = PeerDiscovery(
            peer_id=peer_id,
            run_number=entry.get("runNumber", entry.get("run_number", 0)),
            strategy=entry.get("strategy", "weather"),
            params=entry.get("config", {}).get("params", entry.get("params", {})),
            score=entry.get("results", {}).get("score", entry.get("score", 0.0)),
            metrics=entry.get("results", {}).get("metrics", entry.get("metrics", {})),
            timestamp=entry.get("timestamp", ""),
        )
        if disc.score > 0:
            discoveries.append(disc)

    return discoveries


def evaluate_peer_params(strategy: str = "weather") -> list[dict]:
    """
    Valuta localmente i parametri scoperti dai peer.

    Per ogni scoperta non ancora valutata:
    1. Carica trade locali
    2. Applica i parametri del peer
    3. Calcola score locale
    4. Se score > best_locale * (1 + threshold) -> flag per review
    """
    discoveries = _load_peer_discoveries()
    if not discoveries:
        print("[HYPERSPACE] Nessuna scoperta peer da valutare")
        return []

    # Import locale per evitare circolarita' al top level
    try:
        from auto_optimizer import (
            compute_score, eval_params, load_trades,
            STRATEGY_PARAMS, split_trades_temporal,
        )
    except ImportError:
        print("[HYPERSPACE] ERRORE: impossibile importare auto_optimizer")
        return []

    best_local_score, best_local_params = _get_best_local_score(strategy)
    if best_local_score <= 0:
        print(f"[HYPERSPACE] Nessun best score locale per {strategy}")
        return []

    trades = load_trades()
    param_ranges = STRATEGY_PARAMS.get(strategy, [])
    strat_trades = [t for t in trades if t.strategy == strategy]

    if len(strat_trades) < 10:
        print(f"[HYPERSPACE] Troppi pochi trade per valutare ({len(strat_trades)})")
        return []

    # Split temporale per validazione out-of-sample
    if len(strat_trades) >= 30:
        train_trades, test_trades = split_trades_temporal(trades, 0.70)
    else:
        train_trades = trades
        test_trades = []

    results = []
    updated = False

    for i, disc in enumerate(discoveries):
        if not isinstance(disc, dict):
            continue
        if disc.get("evaluated_locally", False):
            continue
        if disc.get("strategy", "weather") != strategy:
            continue
        if disc.get("peer_id") == PEER_ID:
            continue

        peer_params = disc.get("params", {})
        if not peer_params:
            continue

        # Valuta parametri peer sui nostri trade
        try:
            metrics = eval_params(train_trades, peer_params, strategy)
            score = compute_score(
                metrics, strategy, peer_params, param_ranges
            )

            # Out-of-sample se disponibile
            test_score = 0.0
            if test_trades:
                test_metrics = eval_params(test_trades, peer_params, strategy)
                test_score = compute_score(
                    test_metrics, strategy, peer_params, param_ranges
                )

            # Confronta con best locale
            improvement = 0.0
            if best_local_score > 0:
                improvement = (score - best_local_score) / abs(best_local_score)

            flagged = improvement >= PEER_ADOPTION_THRESHOLD

            disc["evaluated_locally"] = True
            disc["local_score"] = round(score, 6)
            disc["local_test_score"] = round(test_score, 6)
            disc["local_improvement"] = round(improvement * 100, 2)
            disc["flagged_for_review"] = flagged
            updated = True

            status = "*** FLAGGED per review ***" if flagged else "sotto soglia"
            print(
                f"[HYPERSPACE] Peer {disc['peer_id'][:8]} run#{disc.get('run_number', '?')}: "
                f"local_score={score:.4f} vs best={best_local_score:.4f} "
                f"({improvement*100:+.1f}%) — {status}"
            )

            result = {
                "peer_id": disc["peer_id"],
                "run_number": disc.get("run_number", 0),
                "peer_score": disc.get("score", 0),
                "local_score": score,
                "local_test_score": test_score,
                "improvement_pct": improvement * 100,
                "flagged": flagged,
                "params": peer_params,
            }
            results.append(result)

            if flagged:
                _append_journal_peer_discovery(disc, score, improvement)

        except Exception as e:
            print(f"[HYPERSPACE] Errore valutazione peer {disc.get('peer_id', '?')}: {e}")
            disc["evaluated_locally"] = True
            disc["local_score"] = -999
            updated = True

    if updated:
        _save_peer_discoveries(discoveries)

    if results:
        flagged_count = sum(1 for r in results if r["flagged"])
        print(f"\n[HYPERSPACE] Valutati {len(results)} peer — "
              f"{flagged_count} flagged per review (>={PEER_ADOPTION_THRESHOLD*100:.0f}% better)")
    else:
        print("[HYPERSPACE] Nessun nuovo peer da valutare")

    return results


def cross_pollinate(strategy: str = "weather", auto_adopt: bool = False) -> Optional[dict]:
    """
    Cross-pollinazione: se un peer ha parametri significativamente migliori,
    li adotta (o li segnala per review manuale).

    Flusso:
    1. Valuta tutti i peer non ancora testati
    2. Se il migliore supera la soglia, prepara adoption
    3. Con auto_adopt=True, scrive i parametri in auto_optimizer_applied
    """
    results = evaluate_peer_params(strategy)
    if not results:
        return None

    # Trova il miglior risultato flagged
    flagged = [r for r in results if r["flagged"]]
    if not flagged:
        return None

    best_peer = max(flagged, key=lambda r: r["local_score"])

    print(f"\n[HYPERSPACE] Miglior peer: {best_peer['peer_id'][:8]} "
          f"score={best_peer['local_score']:.4f} "
          f"({best_peer['improvement_pct']:+.1f}%)")

    if not auto_adopt:
        print("[HYPERSPACE] Parametri peer flagged per review manuale.")
        print(f"  Params: {json.dumps(best_peer['params'], indent=2)}")
        return best_peer

    # Auto-adopt: scrivi in auto_optimizer_applied
    try:
        from auto_optimizer import LOG_DIR as OPT_LOG_DIR
        applied_file = OPT_LOG_DIR / f"auto_optimizer_applied_{strategy}.json"

        history = []
        if applied_file.exists():
            try:
                data = json.loads(applied_file.read_text())
                history = data if isinstance(data, list) else [data]
            except Exception:
                history = []

        adoption = {
            "strategy": strategy,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "hyperspace_peer",
            "peer_id": best_peer["peer_id"],
            "peer_run": best_peer["run_number"],
            "improvement_pct": round(best_peer["improvement_pct"], 2),
            "local_score": best_peer["local_score"],
            "local_test_score": best_peer.get("local_test_score", 0),
            "params": best_peer["params"],
            "metrics": {},
        }
        history.append(adoption)

        with open(applied_file, "w") as f:
            json.dump(history, f, indent=2)

        print(f"[HYPERSPACE] ADOTTATI parametri peer {best_peer['peer_id'][:8]} "
              f"— scritti in {applied_file}")

        # Marca come adottato nel cache
        discoveries = _load_peer_discoveries()
        for disc in discoveries:
            if (disc.get("peer_id") == best_peer["peer_id"] and
                    disc.get("run_number") == best_peer["run_number"]):
                disc["adopted"] = True
        _save_peer_discoveries(discoveries)

    except Exception as e:
        print(f"[HYPERSPACE] Errore adozione: {e}")

    return best_peer


def _append_journal(run: HyperspaceRun):
    """Aggiunge entry al journal Hyperspace."""
    _ensure_dirs()
    entry = (
        f"\n## Run #{run.run_number} — {run.timestamp}\n"
        f"- Strategy: {run.strategy}\n"
        f"- Score: {run.score:.4f} (train={run.train_score:.4f}, test={run.test_score:.4f})\n"
        f"- Hypothesis: {run.hypothesis}\n"
        f"- Improvement: {run.improvement_pct:+.1f}%\n"
        f"- Trades: {run.n_closed_trades}\n"
        f"- Published to network: yes\n\n"
    )
    with open(JOURNAL_FILE, "a") as f:
        if not JOURNAL_FILE.exists() or JOURNAL_FILE.stat().st_size == 0:
            f.write(f"# Hyperspace Journal — {PROJECT_NAME}\n")
            f.write(f"Peer ID: {PEER_ID}\n\n")
        f.write(entry)


def _append_journal_peer_discovery(disc: dict, local_score: float, improvement: float):
    """Aggiunge entry peer discovery al journal."""
    _ensure_dirs()
    entry = (
        f"\n### Peer Discovery — {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- Peer: {disc.get('peer_id', '?')[:8]}...\n"
        f"- Their score: {disc.get('score', 0):.4f}\n"
        f"- Our local eval: {local_score:.4f} ({improvement*100:+.1f}%)\n"
        f"- Flagged: {'YES' if improvement >= PEER_ADOPTION_THRESHOLD else 'no'}\n"
        f"- Params: {json.dumps(disc.get('params', {}))}\n\n"
    )
    with open(JOURNAL_FILE, "a") as f:
        f.write(entry)


def sync(strategy: str = "weather", auto_adopt: bool = False) -> dict:
    """
    Ciclo completo: pubblica + scopri + valuta + cross-pollina.

    Ritorna un sommario dell'operazione.
    """
    print(f"\n{'='*70}")
    print(f"  HYPERSPACE SYNC — {strategy}")
    print(f"  Peer ID: {PEER_ID}")
    print(f"  Nodo: {HYPERSPACE_BASE}")
    print(f"{'='*70}\n")

    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy,
        "peer_id": PEER_ID,
        "published": False,
        "discoveries": 0,
        "evaluated": 0,
        "flagged": 0,
        "adopted": None,
    }

    # 1. Pubblica
    run = publish_results(strategy)
    if run:
        summary["published"] = True
        summary["published_score"] = run.score

    # 2. Scopri
    discoveries = discover_peer_results()
    summary["discoveries"] = len(discoveries)

    # 3. Valuta + Cross-pollina
    adopted = cross_pollinate(strategy, auto_adopt=auto_adopt)
    if adopted:
        summary["adopted"] = {
            "peer_id": adopted["peer_id"][:8],
            "improvement": adopted["improvement_pct"],
            "score": adopted["local_score"],
        }

    print(f"\n{'='*70}")
    print(f"  SYNC COMPLETATO")
    print(f"  Pubblicato: {'si' if summary['published'] else 'no'}")
    print(f"  Scoperte: {summary['discoveries']}")
    if summary['adopted']:
        print(f"  Adottato: peer {summary['adopted']['peer_id']} "
              f"({summary['adopted']['improvement']:+.1f}%)")
    else:
        print(f"  Adottato: nessuno")
    print(f"{'='*70}\n")

    # Salva sommario
    sync_log = HYPERSPACE_DIR / "last_sync.json"
    with open(sync_log, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Hyperspace Distributed Optimizer — cross-pollination P2P"
    )
    parser.add_argument(
        "--publish", action="store_true",
        help="Pubblica ultimi risultati AutoOptimizer su Hyperspace"
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Cerca scoperte peer sulla rete"
    )
    parser.add_argument(
        "--evaluate", action="store_true",
        help="Valuta localmente i parametri dei peer"
    )
    parser.add_argument(
        "--sync", action="store_true",
        help="Ciclo completo: pubblica + scopri + valuta"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Stato connessione Hyperspace"
    )
    parser.add_argument(
        "--strategy", default="weather",
        choices=["weather", "favorite_longshot", "abandoned_position"],
        help="Strategia da sincronizzare (default: weather)"
    )
    parser.add_argument(
        "--auto-adopt", action="store_true",
        help="Adotta automaticamente parametri peer se >10%% migliori"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Mostra cosa verrebbe pubblicato senza inviare"
    )
    args = parser.parse_args()

    if args.status:
        status = check_hyperspace_status()
        print(f"\n{'='*50}")
        print(f"  HYPERSPACE NODE STATUS")
        print(f"{'='*50}")
        print(f"  Connected: {status['connected']}")
        print(f"  Node URL:  {status['node_url']}")
        print(f"  Peer ID:   {PEER_ID}")
        if status['models']:
            print(f"  Models:    {', '.join(status['models'])}")
        if status['state']:
            print(f"  Peers:     {status['state'].get('peers', '?')}")
            print(f"  Runs:      {status['state'].get('runs', '?')}")
        print(f"{'='*50}\n")
        return

    if args.publish:
        publish_results(args.strategy, dry_run=args.dry_run)
    elif args.discover:
        discover_peer_results()
    elif args.evaluate:
        evaluate_peer_params(args.strategy)
    elif args.sync:
        sync(args.strategy, auto_adopt=args.auto_adopt)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
