"""
Replication Score — Misura la fedeltà di replica del copy trading
================================================================
Modulo che quantifica quanto bene il bot replica il pattern di trading
di un whale target. Basato sulla distanza L1 tra distribuzioni normalizzate,
ispirato dall'analisi di replicazione in polybot (research/replication_score.py).

Cinque componenti misurate:
- Market mix: distribuzione dei trade tra mercati
- Outcome mix: distribuzione BUY vs SELL (o YES vs NO)
- Size distribution: distribuzione delle size in bucket quantilici
- Timing distribution: distribuzione dei trade per fascia oraria (5 min)
- Inter-arrival: distribuzione dei tempi tra trade consecutivi

Punteggio finale 0-100 dove 100 = replica perfetta.

Uso standalone:
    python3 -m utils.replication_score       # test con dati sintetici
"""

from __future__ import annotations

import math
import statistics
from typing import Optional


# ── Distanza L1 tra distribuzioni ──


def l1_distance(dist_a: dict[str, float], dist_b: dict[str, float]) -> float:
    """
    Distanza L1 tra due distribuzioni normalizzate.

    Ogni distribuzione e' un dict categoria -> frazione (somma ~1.0).
    La distanza e' la somma dei valori assoluti delle differenze
    su tutte le categorie presenti in almeno una delle due distribuzioni.

    Ritorna float in [0, 2]:
    - 0 = distribuzioni identiche
    - 2 = distribuzioni completamente disgiunte (nessuna categoria in comune)
    """
    keys = set(dist_a) | set(dist_b)
    return sum(abs(dist_a.get(k, 0.0) - dist_b.get(k, 0.0)) for k in keys)


# ── Normalizzazione ──


def normalize_distribution(counts: dict[str, float]) -> dict[str, float]:
    """
    Normalizza un dict di conteggi in una distribuzione che somma a 1.0.

    Prende dict categoria -> conteggio, ritorna dict categoria -> frazione.
    Gestisce il caso di dizionario vuoto (ritorna vuoto) e totale zero.
    """
    if not counts:
        return {}
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in counts.items()}


# ── Componenti della distribuzione ──


def compute_market_mix(trades: list[dict]) -> dict[str, float]:
    """
    Distribuzione dei trade tra i vari mercati.

    Costruisce un conteggio per market_id e lo normalizza.
    Misura se il bot opera sugli stessi mercati del whale e con
    la stessa proporzione relativa.
    """
    counts: dict[str, float] = {}
    for t in trades:
        mid = t.get("market_id", "unknown")
        counts[mid] = counts.get(mid, 0) + 1
    return normalize_distribution(counts)


def compute_outcome_mix(trades: list[dict]) -> dict[str, float]:
    """
    Distribuzione dei trade per side (BUY/SELL, YES/NO).

    Normalizza il side in uppercase e raggruppa. Misura se il bot
    mantiene lo stesso rapporto buy/sell del whale.
    """
    counts: dict[str, float] = {}
    for t in trades:
        side = str(t.get("side", "UNKNOWN")).upper()
        counts[side] = counts.get(side, 0) + 1
    return normalize_distribution(counts)


def compute_size_distribution(
    trades: list[dict], n_buckets: int = 10
) -> dict[str, float]:
    """
    Distribuzione delle size dei trade in bucket basati sui quantili.

    Divide il range delle size in n_buckets usando i percentili
    calcolati sulle size stesse, poi conta quanti trade cadono
    in ogni bucket. Misura se il bot usa size simili a quelle del whale.
    """
    sizes = [float(t.get("size", 0)) for t in trades if float(t.get("size", 0)) > 0]
    if not sizes:
        return {}

    sizes_sorted = sorted(sizes)
    n = len(sizes_sorted)

    if n < n_buckets:
        # Meno trade che bucket: ogni trade e' un bucket
        counts: dict[str, float] = {}
        for s in sizes_sorted:
            label = f"{s:.2f}"
            counts[label] = counts.get(label, 0) + 1
        return normalize_distribution(counts)

    # Calcola i boundary dei bucket via quantili
    boundaries = []
    for i in range(n_buckets + 1):
        idx = int(i * (n - 1) / n_buckets)
        boundaries.append(sizes_sorted[idx])

    # Conta i trade per bucket
    counts = {}
    for s in sizes:
        assigned = False
        for b in range(n_buckets):
            lo = boundaries[b]
            hi = boundaries[b + 1]
            if b < n_buckets - 1:
                if lo <= s < hi:
                    label = f"[{lo:.2f},{hi:.2f})"
                    counts[label] = counts.get(label, 0) + 1
                    assigned = True
                    break
            else:
                # Ultimo bucket include il limite superiore
                if lo <= s <= hi:
                    label = f"[{lo:.2f},{hi:.2f}]"
                    counts[label] = counts.get(label, 0) + 1
                    assigned = True
                    break
        if not assigned:
            # Fallback: ultimo bucket
            label = f"[{boundaries[-2]:.2f},{boundaries[-1]:.2f}]"
            counts[label] = counts.get(label, 0) + 1

    return normalize_distribution(counts)


def compute_timing_distribution(
    trades: list[dict], bucket_seconds: int = 300
) -> dict[str, float]:
    """
    Distribuzione dei trade per fascia oraria (time-of-day).

    Raggruppa i timestamp in bucket di bucket_seconds secondi
    (default 5 minuti = 300s) calcolati come offset dall'inizio
    del giorno (UTC). 288 bucket possibili per giorno.

    Misura se il bot opera nelle stesse fasce orarie del whale.
    """
    counts: dict[str, float] = {}
    for t in trades:
        ts = float(t.get("timestamp", 0))
        if ts <= 0:
            continue
        # Secondi dall'inizio del giorno (UTC)
        seconds_in_day = ts % 86400
        bucket_start = int(seconds_in_day // bucket_seconds) * bucket_seconds
        # Formatta come HH:MM
        hours = bucket_start // 3600
        minutes = (bucket_start % 3600) // 60
        label = f"{hours:02d}:{minutes:02d}"
        counts[label] = counts.get(label, 0) + 1
    return normalize_distribution(counts)


def compute_interarrival_distribution(
    trades: list[dict],
    buckets: Optional[list[float]] = None,
) -> dict[str, float]:
    """
    Distribuzione degli inter-arrival time tra trade consecutivi.

    Calcola il tempo in secondi tra ogni coppia di trade consecutivi
    (ordinati per timestamp) e li raggruppa in bucket predefiniti.
    I bucket rappresentano le soglie in secondi: 0-1s, 1-2s, 2-5s, ecc.

    Misura se il bot ha una latenza di esecuzione simile a quella del whale.
    """
    if buckets is None:
        buckets = [0, 1, 2, 5, 10, 30, 60, 120, 300, 600]

    sorted_trades = sorted(trades, key=lambda t: float(t.get("timestamp", 0)))

    deltas: list[float] = []
    for i in range(1, len(sorted_trades)):
        ts_prev = float(sorted_trades[i - 1].get("timestamp", 0))
        ts_curr = float(sorted_trades[i].get("timestamp", 0))
        if ts_prev > 0 and ts_curr > ts_prev:
            deltas.append(ts_curr - ts_prev)

    if not deltas:
        return {}

    counts: dict[str, float] = {}
    for d in deltas:
        assigned = False
        for j in range(len(buckets) - 1):
            lo = buckets[j]
            hi = buckets[j + 1]
            if lo <= d < hi:
                label = f"{lo:.0f}-{hi:.0f}s"
                counts[label] = counts.get(label, 0) + 1
                assigned = True
                break
        if not assigned:
            # Oltre l'ultimo bucket
            label = f">{buckets[-1]:.0f}s"
            counts[label] = counts.get(label, 0) + 1

    return normalize_distribution(counts)


# ── Scorer ──


class ReplicationScorer:
    """
    Calcola il Replication Score complessivo tra whale e bot.

    Il punteggio combina 5 componenti distribuzionali misurate
    con distanza L1. Ogni componente ha range [0, 2]; il punteggio
    finale e' mappato su [0, 100] dove 100 = replica perfetta.

    Formula: score = max(0, 100 * (1 - avg_l1 / 2))
    """

    def __init__(self, whale_trades: list[dict], bot_trades: list[dict]):
        """
        Inizializza lo scorer con i trade del whale e del bot.

        Ogni trade e' un dict con chiavi:
        market_id, side, price, size, timestamp, question (opzionale).
        """
        self._whale_trades = whale_trades
        self._bot_trades = bot_trades
        self._components: Optional[dict[str, float]] = None

    def component_scores(self) -> dict[str, float]:
        """
        Ritorna le distanze L1 per ogni componente.

        Dict con chiavi: market_mix, outcome_mix, size, timing, interarrival.
        Ogni valore e' in [0, 2] dove 0 = identiche.
        """
        if self._components is not None:
            return self._components

        w = self._whale_trades
        b = self._bot_trades

        self._components = {
            "market_mix": l1_distance(compute_market_mix(w), compute_market_mix(b)),
            "outcome_mix": l1_distance(compute_outcome_mix(w), compute_outcome_mix(b)),
            "size": l1_distance(
                compute_size_distribution(w), compute_size_distribution(b)
            ),
            "timing": l1_distance(
                compute_timing_distribution(w), compute_timing_distribution(b)
            ),
            "interarrival": l1_distance(
                compute_interarrival_distribution(w),
                compute_interarrival_distribution(b),
            ),
        }
        return self._components

    def score(self) -> float:
        """
        Punteggio di replicazione complessivo (0-100).

        Media delle 5 componenti L1 convertita in scala 0-100:
        score = max(0, 100 * (1 - avg_l1 / 2))

        100 = replica perfetta (tutte le L1 = 0)
        0   = replica completamente divergente (tutte le L1 = 2)
        """
        components = self.component_scores()
        if not components:
            return 0.0
        avg_l1 = sum(components.values()) / len(components)
        return max(0.0, 100.0 * (1.0 - avg_l1 / 2.0))

    def report(self) -> str:
        """
        Report leggibile con punteggio complessivo e dettaglio componenti.

        Formato tabellare con indicazione visuale del livello di match
        per ogni componente: OTTIMO (L1<0.2), BUONO (<0.5), MEDIO (<1.0),
        SCARSO (>=1.0).
        """
        components = self.component_scores()
        overall = self.score()

        lines = []
        lines.append("=" * 60)
        lines.append("  REPLICATION SCORE REPORT")
        lines.append("=" * 60)
        lines.append(f"  Whale trades: {len(self._whale_trades)}")
        lines.append(f"  Bot trades:   {len(self._bot_trades)}")
        lines.append("")
        lines.append(f"  {'Componente':<20} {'L1':>6}  {'Livello':<10}")
        lines.append("-" * 60)

        for name, l1_val in components.items():
            if l1_val < 0.2:
                level = "OTTIMO"
            elif l1_val < 0.5:
                level = "BUONO"
            elif l1_val < 1.0:
                level = "MEDIO"
            else:
                level = "SCARSO"
            lines.append(f"  {name:<20} {l1_val:>6.3f}  {level:<10}")

        lines.append("-" * 60)
        lines.append(f"  Replication Score: {overall:.1f}/100")
        lines.append("=" * 60)
        return "\n".join(lines)


# ── Trade Matching ──


def compare_trade_matching(
    whale_trades: list[dict],
    bot_trades: list[dict],
    max_delta_s: float = 60,
    price_eps: float = 0.005,
) -> dict:
    """
    Confronto diretto trade-per-trade tra whale e bot.

    Matcha i trade del whale con quelli del bot usando criteri:
    - Stesso market_id
    - Stesso side
    - Prezzo entro price_eps
    - Timestamp entro max_delta_s secondi

    Ogni trade del bot puo' essere matchato al massimo una volta
    (greedy matching per timestamp piu' vicino).

    Ritorna dict con:
    - matched_count: numero di trade matchati
    - unmatched_whale: trade whale senza corrispondente bot
    - unmatched_bot: trade bot senza corrispondente whale
    - recall: matched / whale_total (copertura del whale)
    - precision: matched / bot_total (precisione del bot)
    - median_time_delta: mediana del delta temporale dei match (secondi)
    """
    if not whale_trades or not bot_trades:
        return {
            "matched_count": 0,
            "unmatched_whale": len(whale_trades),
            "unmatched_bot": len(bot_trades),
            "recall": 0.0,
            "precision": 0.0,
            "median_time_delta": None,
        }

    # Ordina entrambi per timestamp per efficienza
    w_sorted = sorted(whale_trades, key=lambda t: float(t.get("timestamp", 0)))
    b_sorted = sorted(bot_trades, key=lambda t: float(t.get("timestamp", 0)))

    # Set di indici bot gia' matchati
    matched_bot_indices: set[int] = set()
    time_deltas: list[float] = []
    matched_count = 0

    for wt in w_sorted:
        w_market = wt.get("market_id", "")
        w_side = str(wt.get("side", "")).upper()
        w_price = float(wt.get("price", 0))
        w_ts = float(wt.get("timestamp", 0))

        best_idx: Optional[int] = None
        best_delta = float("inf")

        for bi, bt in enumerate(b_sorted):
            if bi in matched_bot_indices:
                continue

            b_market = bt.get("market_id", "")
            b_side = str(bt.get("side", "")).upper()
            b_price = float(bt.get("price", 0))
            b_ts = float(bt.get("timestamp", 0))

            # Criterio 1: stesso mercato
            if b_market != w_market:
                continue

            # Criterio 2: stesso side
            if b_side != w_side:
                continue

            # Criterio 3: prezzo entro epsilon
            if abs(b_price - w_price) > price_eps:
                continue

            # Criterio 4: timestamp entro max_delta_s
            delta = abs(b_ts - w_ts)
            if delta > max_delta_s:
                continue

            # Prendi il match con delta piu' piccolo
            if delta < best_delta:
                best_delta = delta
                best_idx = bi

        if best_idx is not None:
            matched_bot_indices.add(best_idx)
            time_deltas.append(best_delta)
            matched_count += 1

    n_whale = len(whale_trades)
    n_bot = len(bot_trades)

    median_delta: Optional[float] = None
    if time_deltas:
        median_delta = statistics.median(time_deltas)

    return {
        "matched_count": matched_count,
        "unmatched_whale": n_whale - matched_count,
        "unmatched_bot": n_bot - matched_count,
        "recall": matched_count / n_whale if n_whale > 0 else 0.0,
        "precision": matched_count / n_bot if n_bot > 0 else 0.0,
        "median_time_delta": median_delta,
    }


# ── Main: test con dati sintetici ──


if __name__ == "__main__":
    import random
    import time as _time

    print("Replication Score — Test con dati sintetici")
    print()

    random.seed(42)
    base_ts = _time.time() - 86400  # 24h fa

    markets = ["mkt_btc_up", "mkt_eth_up", "mkt_election", "mkt_weather"]
    sides = ["BUY", "SELL"]

    # Genera trade whale: 100 trade nell'ultimo giorno
    whale_trades = []
    for i in range(100):
        whale_trades.append({
            "market_id": random.choice(markets),
            "side": random.choices(sides, weights=[0.65, 0.35])[0],
            "price": round(random.uniform(0.30, 0.90), 3),
            "size": round(random.uniform(10, 500), 2),
            "timestamp": base_ts + i * random.uniform(200, 1200),
            "question": f"Test market {i}",
        })

    # Genera trade bot: copia imperfetta del whale
    # ~70% dei trade whale vengono copiati, con rumore
    bot_trades = []
    for wt in whale_trades:
        if random.random() < 0.70:
            # Copia con piccole variazioni
            bot_trades.append({
                "market_id": wt["market_id"],
                "side": wt["side"],
                "price": round(wt["price"] + random.uniform(-0.01, 0.01), 3),
                "size": round(wt["size"] * random.uniform(0.08, 0.12), 2),
                "timestamp": wt["timestamp"] + random.uniform(5, 45),
                "question": wt.get("question", ""),
            })

    # Aggiungi qualche trade extra del bot (non matchato al whale)
    for _ in range(10):
        bot_trades.append({
            "market_id": random.choice(markets),
            "side": random.choice(sides),
            "price": round(random.uniform(0.30, 0.90), 3),
            "size": round(random.uniform(5, 50), 2),
            "timestamp": base_ts + random.uniform(0, 86400),
            "question": "Extra bot trade",
        })

    # Calcola replication score
    scorer = ReplicationScorer(whale_trades, bot_trades)
    print(scorer.report())
    print()

    # Trade matching
    matching = compare_trade_matching(whale_trades, bot_trades)
    print("TRADE MATCHING RESULTS:")
    print(f"  Matched:          {matching['matched_count']}")
    print(f"  Unmatched whale:  {matching['unmatched_whale']}")
    print(f"  Unmatched bot:    {matching['unmatched_bot']}")
    print(f"  Recall:           {matching['recall']:.1%}")
    print(f"  Precision:        {matching['precision']:.1%}")
    if matching["median_time_delta"] is not None:
        print(f"  Median time delta: {matching['median_time_delta']:.1f}s")
    print()

    # Test edge case: distribuzioni identiche
    scorer_perfect = ReplicationScorer(whale_trades, whale_trades)
    print(f"Self-replication score (sanity check): {scorer_perfect.score():.1f}/100")

    # Test edge case: nessun trade
    scorer_empty = ReplicationScorer([], [])
    print(f"Empty trades score: {scorer_empty.score():.1f}/100")

    # Test singole funzioni
    print()
    print("L1 distance test:")
    d1 = {"a": 0.5, "b": 0.3, "c": 0.2}
    d2 = {"a": 0.4, "b": 0.4, "c": 0.2}
    print(f"  l1_distance({d1}, {d2}) = {l1_distance(d1, d2):.3f}")

    d3 = {"a": 1.0}
    d4 = {"b": 1.0}
    print(f"  l1_distance({d3}, {d4}) = {l1_distance(d3, d4):.3f}  (max disjoint)")

    print()
    print("normalize_distribution test:")
    raw = {"BUY": 65, "SELL": 35}
    norm = normalize_distribution(raw)
    print(f"  {raw} -> {norm}")
    print(f"  Sum: {sum(norm.values()):.4f}")
