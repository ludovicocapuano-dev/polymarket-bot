#!/usr/bin/env python3
"""
Market Intelligence — Analisi semantica dei mercati Polymarket.
================================================================
Usa embedding locali (Hyperspace) per:
1. Trovare cluster di mercati correlati (rischio concentrazione)
2. Scoprire mercati non coperti dal bot
3. Analizzare similarita' tra mercati attivi

Uso standalone:
    python3 scripts/market_intelligence.py

Uso come modulo (importato dal bot):
    from scripts.market_intelligence import run_market_intelligence
    report = run_market_intelligence(markets, active_titles)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime

# Aggiungi root del progetto al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.market_embeddings import (
    detect_correlation_clusters,
    discover_uncovered_markets,
    find_similar_markets,
    get_cache_stats,
)

logger = logging.getLogger(__name__)

# ── Keywords per filtrare mercati weather ───────────────────────

WEATHER_KEYWORDS = [
    "temperature", "highest temp", "lowest temp", "rain", "snow",
    "wind", "precipitation", "weather", "degrees", "fahrenheit",
    "celsius", "forecast", "humidity",
]


def _is_weather_market(question: str) -> bool:
    """Controlla se un mercato e' weather-related."""
    q_lower = question.lower()
    return any(kw in q_lower for kw in WEATHER_KEYWORDS)


def _fetch_markets_from_api() -> list[dict]:
    """Fetch mercati da Gamma API (standalone, senza autenticazione CLOB)."""
    import requests

    all_markets = []
    for offset in range(0, 600, 100):
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "order": "volume",
                    "ascending": "false",
                    "offset": offset,
                },
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_markets.extend(batch)
        except Exception as e:
            logger.warning(f"[INTEL] Errore fetch offset={offset}: {e}")
            break

    return all_markets


def _get_active_market_titles(risk_manager=None) -> list[str]:
    """
    Ottieni titoli dei mercati su cui il bot ha posizioni aperte.
    Se risk_manager disponibile, usa quello. Altrimenti legge trades.json.
    """
    if risk_manager:
        return [t.market_title for t in risk_manager.open_trades
                if hasattr(t, "market_title") and t.market_title]

    # Fallback: leggi da trades.json
    trades_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "trades.json"
    )
    if not os.path.exists(trades_path):
        return []

    try:
        with open(trades_path) as f:
            trades = json.load(f)
        # Filtra trade aperti (no exit_price)
        active = [
            t.get("market_title", t.get("question", ""))
            for t in trades
            if not t.get("exit_price") and not t.get("closed")
        ]
        return [t for t in active if t]
    except Exception as e:
        logger.warning(f"[INTEL] Errore lettura trades.json: {e}")
        return []


def run_market_intelligence(
    markets: list[dict] = None,
    active_titles: list[str] = None,
    risk_manager=None,
    correlation_threshold: float = 0.85,
    uncovered_min_similarity: float = 0.5,
) -> dict:
    """
    Esegue l'analisi completa di market intelligence.

    Args:
        markets: lista di mercati (dict con 'question'). Se None, fetcha da API.
        active_titles: titoli mercati attivi. Se None, legge da risk_manager o trades.json.
        risk_manager: istanza RiskManager (opzionale)
        correlation_threshold: soglia per clustering (default 0.85)
        uncovered_min_similarity: soglia per mercati non coperti (default 0.5)

    Returns:
        Dict con report completo:
        {
            "timestamp": str,
            "total_markets": int,
            "weather_markets": int,
            "active_markets": int,
            "correlation_clusters": list[list[str]],
            "uncovered_opportunities": list[str],
            "cache_stats": dict,
            "duration_seconds": float,
        }
    """
    t0 = time.time()

    # Fetch mercati se non forniti
    if markets is None:
        logger.info("[INTEL] Fetching mercati da Gamma API...")
        raw_markets = _fetch_markets_from_api()
        markets = [
            {"question": m.get("question", m.get("title", "")), **m}
            for m in raw_markets
            if m.get("question") or m.get("title")
        ]
        logger.info(f"[INTEL] {len(markets)} mercati fetchati")

    # Filtra weather markets
    weather_markets = [
        m for m in markets
        if _is_weather_market(m.get("question", m.get("title", "")))
    ]

    all_titles = [
        m.get("question", m.get("title", ""))
        for m in markets if m.get("question") or m.get("title")
    ]

    weather_titles = [
        m.get("question", m.get("title", ""))
        for m in weather_markets
    ]

    # Ottieni mercati attivi
    if active_titles is None:
        active_titles = _get_active_market_titles(risk_manager)

    logger.info(
        f"[INTEL] Analisi: {len(markets)} totali, "
        f"{len(weather_markets)} weather, {len(active_titles)} attivi"
    )

    # 1. Correlation clusters (su weather markets)
    clusters = []
    if len(weather_markets) >= 2:
        try:
            clusters = detect_correlation_clusters(
                weather_markets, threshold=correlation_threshold
            )
            for i, cluster in enumerate(clusters):
                logger.info(
                    f"[INTEL] Cluster #{i+1} ({len(cluster)} mercati): "
                    f"{cluster[0][:60]}... + {len(cluster)-1} simili"
                )
        except RuntimeError as e:
            logger.warning(f"[INTEL] Errore clustering: {e}")

    # 2. Uncovered opportunities
    uncovered = []
    if active_titles and weather_titles:
        try:
            uncovered = discover_uncovered_markets(
                active_titles, weather_titles,
                min_similarity=uncovered_min_similarity,
            )
            if uncovered:
                logger.info(
                    f"[INTEL] {len(uncovered)} mercati weather non coperti trovati"
                )
                for title in uncovered[:5]:
                    logger.info(f"[INTEL]   -> {title[:80]}")
        except RuntimeError as e:
            logger.warning(f"[INTEL] Errore discovery: {e}")

    # 3. Cross-strategy similarity (weather vs non-weather)
    # Trova mercati non-weather che sono simili a quelli weather
    # Potenziali candidati per favorite_longshot o resolution_sniper
    non_weather_similar = []
    if weather_titles and len(all_titles) > len(weather_titles):
        non_weather = [t for t in all_titles if t not in set(weather_titles)]
        if non_weather and weather_titles:
            try:
                # Prendi il titolo weather piu' rappresentativo (primo per volume)
                sample_weather = weather_titles[0]
                similar = find_similar_markets(
                    sample_weather, non_weather, top_k=5
                )
                non_weather_similar = [
                    (t, s) for t, s in similar if s >= 0.4
                ]
                if non_weather_similar:
                    logger.info(
                        f"[INTEL] {len(non_weather_similar)} mercati non-weather "
                        f"simili a weather trovati"
                    )
            except RuntimeError:
                pass  # Non critico

    duration = time.time() - t0
    cache = get_cache_stats()

    report = {
        "timestamp": datetime.now().isoformat(),
        "total_markets": len(markets),
        "weather_markets": len(weather_markets),
        "active_markets": len(active_titles),
        "correlation_clusters": clusters,
        "n_clusters": len(clusters),
        "uncovered_opportunities": uncovered[:20],  # top 20
        "n_uncovered": len(uncovered),
        "cross_strategy_similar": [
            {"title": t, "similarity": round(s, 3)}
            for t, s in non_weather_similar
        ],
        "cache_stats": cache,
        "duration_seconds": round(duration, 2),
    }

    logger.info(
        f"[INTEL] Report completato in {duration:.1f}s: "
        f"{len(clusters)} cluster, {len(uncovered)} uncovered, "
        f"cache={cache['cached_embeddings']} embeddings"
    )

    return report


def print_report(report: dict):
    """Stampa il report in formato leggibile."""
    print(f"\n{'='*70}")
    print(f"  MARKET INTELLIGENCE REPORT — {report['timestamp']}")
    print(f"{'='*70}")
    print(f"  Mercati analizzati: {report['total_markets']}")
    print(f"  Mercati weather:    {report['weather_markets']}")
    print(f"  Posizioni attive:   {report['active_markets']}")
    print(f"  Tempo analisi:      {report['duration_seconds']}s")
    print(f"{'─'*70}")

    # Cluster
    print(f"\n  CORRELATION CLUSTERS ({report['n_clusters']} trovati):")
    if report["correlation_clusters"]:
        for i, cluster in enumerate(report["correlation_clusters"]):
            print(f"\n  Cluster #{i+1} ({len(cluster)} mercati):")
            for title in cluster[:5]:
                print(f"    - {title[:70]}")
            if len(cluster) > 5:
                print(f"    ... e {len(cluster)-5} altri")
    else:
        print("    Nessun cluster trovato (mercati poco correlati)")

    # Uncovered
    print(f"\n  UNCOVERED OPPORTUNITIES ({report['n_uncovered']} trovati):")
    if report["uncovered_opportunities"]:
        for title in report["uncovered_opportunities"][:10]:
            print(f"    - {title[:70]}")
        if report["n_uncovered"] > 10:
            print(f"    ... e {report['n_uncovered']-10} altri")
    else:
        print("    Tutti i mercati affini sono coperti")

    # Cross-strategy
    if report.get("cross_strategy_similar"):
        print(f"\n  CROSS-STRATEGY SIMILAR ({len(report['cross_strategy_similar'])}):")
        for item in report["cross_strategy_similar"]:
            print(f"    - [{item['similarity']:.2f}] {item['title'][:65]}")

    # Cache
    cs = report["cache_stats"]
    print(f"\n  Cache: {cs['cached_embeddings']} embeddings (TTL {cs['ttl_seconds']}s)")
    print(f"{'='*70}\n")


def save_report(report: dict):
    """Salva il report in logs/market_intelligence.json."""
    logs_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
    )
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, "market_intelligence.json")

    try:
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"[INTEL] Report salvato in {path}")
    except Exception as e:
        logger.warning(f"[INTEL] Errore salvataggio report: {e}")


# ── Standalone ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Market Intelligence Report")
    parser.add_argument(
        "--threshold", type=float, default=0.85,
        help="Soglia correlazione per clustering (default: 0.85)"
    )
    parser.add_argument(
        "--min-similarity", type=float, default=0.5,
        help="Soglia minima similarita' per discovery (default: 0.5)"
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Salva report in logs/market_intelligence.json"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Solo log, nessun output console"
    )
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    report = run_market_intelligence(
        correlation_threshold=args.threshold,
        uncovered_min_similarity=args.min_similarity,
    )

    if not args.quiet:
        print_report(report)

    if args.save:
        save_report(report)
