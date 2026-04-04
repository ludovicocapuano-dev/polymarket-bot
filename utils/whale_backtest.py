"""
Whale Backtest — Framework Monte Carlo multi-scenario per validazione performance whale
======================================================================================
v1.0: Backtest con block bootstrap, decomposizione PnL e analisi statistica
per validare se le performance osservate dei whale sono statisticamente
significative o frutto di fortuna.

Metriche:
- Block Bootstrap: preserva autocorrelazione temporale nei PnL
- Scenario Analysis: actual / mid / all_maker / all_taker
- PnL Decomposition: directional_alpha vs execution_edge
- Sharpe Ratio annualizzato + Max Drawdown

Uso standalone:
    python3 -m utils.whale_backtest           # test con dati sintetici
    python3 -m utils.whale_backtest -v        # verbose
"""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Funzioni di utilita' ──


def block_bootstrap(
    pnl_array: list | np.ndarray,
    iters: int = 20000,
    block_len: int = 50,
    seed: int = 7,
) -> dict:
    """
    Block bootstrap circolare su un array di PnL.

    Preserva l'autocorrelazione temporale usando blocchi di lunghezza fissa.
    Per ogni iterazione, campiona blocchi circolari e calcola PnL totale
    e max drawdown del path sintetico.

    Parametri:
        pnl_array:  lista o array di PnL per-trade (o per-periodo)
        iters:      numero di iterazioni bootstrap (default 20000)
        block_len:  lunghezza del blocco circolare (default 50)
        seed:       seed per riproducibilita' (default 7)

    Ritorna:
        dict con quantili (p01, p05, p50, p95, p99) per total_pnl e max_drawdown
    """
    pnl = np.asarray(pnl_array, dtype=np.float64)
    n = len(pnl)

    if n == 0:
        return {
            "total_pnl": {"p01": 0.0, "p05": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0},
            "max_drawdown": {"p01": 0.0, "p05": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0},
        }

    rng = np.random.default_rng(seed)

    # Numero di blocchi necessari per coprire almeno n osservazioni
    n_blocks = int(np.ceil(n / block_len))

    # Pre-alloca risultati
    total_pnls = np.empty(iters, dtype=np.float64)
    max_dds = np.empty(iters, dtype=np.float64)

    for i in range(iters):
        # Campiona punti di inizio dei blocchi (circolari)
        starts = rng.integers(0, n, size=n_blocks)

        # Costruisci il path sintetico concatenando blocchi circolari
        indices = np.concatenate([
            np.arange(s, s + block_len) % n for s in starts
        ])[:n]

        path_pnl = pnl[indices]

        # PnL totale del path
        total_pnls[i] = np.sum(path_pnl)

        # Max drawdown dalla equity curve del path
        equity = np.cumsum(path_pnl)
        running_max = np.maximum.accumulate(equity)
        drawdowns = running_max - equity
        max_dds[i] = np.max(drawdowns) if len(drawdowns) > 0 else 0.0

    # Calcola quantili
    quantiles = [1, 5, 50, 95, 99]
    pnl_quantiles = np.percentile(total_pnls, quantiles)
    dd_quantiles = np.percentile(max_dds, quantiles)

    return {
        "total_pnl": {
            f"p{q:02d}": round(float(v), 6)
            for q, v in zip(quantiles, pnl_quantiles)
        },
        "max_drawdown": {
            f"p{q:02d}": round(float(v), 6)
            for q, v in zip(quantiles, dd_quantiles)
        },
    }


def scenario_pnl(trades: list[dict], scenario: str) -> list[float]:
    """
    Calcola il PnL per-trade sotto diversi scenari di esecuzione.

    Scenari disponibili:
        - "actual":     prezzo reale di esecuzione (trade['price'])
        - "mid":        prezzo mid (trade['mid_price'])
        - "all_maker":  best_bid per BUY, best_ask per SELL (massimo rebate)
        - "all_taker":  best_ask per BUY, best_bid per SELL (massimo slippage)

    Ogni trade deve avere le chiavi: price, size, side.
    Chiavi opzionali: settle_price, mid_price, best_bid, best_ask.

    Se settle_price manca, usa l'ultimo prezzo osservato per quel mercato
    (approssimazione: ultimo price nella lista).

    Formula PnL:
        BUY:  size * (settle_price - entry_price)
        SELL: size * (entry_price - settle_price)

    Parametri:
        trades:   lista di dizionari trade
        scenario: stringa scenario ("actual", "mid", "all_maker", "all_taker")

    Ritorna:
        lista di float con PnL per-trade
    """
    valid_scenarios = ("actual", "mid", "all_maker", "all_taker")
    if scenario not in valid_scenarios:
        raise ValueError(
            f"Scenario '{scenario}' non valido. Usa uno tra: {valid_scenarios}"
        )

    if not trades:
        return []

    # Determina l'ultimo prezzo osservato come fallback per settle_price
    # (usa l'ultimo trade price della lista come proxy)
    last_price = trades[-1].get("price", 0.0)

    pnl_list: list[float] = []

    for trade in trades:
        size = float(trade.get("size", 0.0))
        side = str(trade.get("side", "")).upper()
        settle = float(trade.get("settle_price", 0.0))

        # Fallback settle_price: ultimo prezzo osservato
        if settle == 0.0:
            settle = last_price

        # Determina entry_price in base allo scenario
        if scenario == "actual":
            entry = float(trade.get("price", 0.0))
        elif scenario == "mid":
            entry = float(trade.get("mid_price", trade.get("price", 0.0)))
        elif scenario == "all_maker":
            # Maker: miglior prezzo per noi
            # BUY → compriamo al best_bid (prezzo piu' basso)
            # SELL → vendiamo al best_ask (prezzo piu' alto)
            if side == "BUY":
                entry = float(trade.get("best_bid", trade.get("price", 0.0)))
            else:
                entry = float(trade.get("best_ask", trade.get("price", 0.0)))
        elif scenario == "all_taker":
            # Taker: peggior prezzo per noi
            # BUY → compriamo al best_ask (prezzo piu' alto)
            # SELL → vendiamo al best_bid (prezzo piu' basso)
            if side == "BUY":
                entry = float(trade.get("best_ask", trade.get("price", 0.0)))
            else:
                entry = float(trade.get("best_bid", trade.get("price", 0.0)))

        # Calcola PnL
        if side == "BUY":
            pnl = size * (settle - entry)
        elif side == "SELL":
            pnl = size * (entry - settle)
        else:
            # Side sconosciuto — tratta come BUY
            pnl = size * (settle - entry)

        pnl_list.append(pnl)

    return pnl_list


def pnl_decomposition(trades: list[dict]) -> dict:
    """
    Decompone il PnL totale in due componenti:

    1. directional_alpha: profitto dalla previsione direzionale
       - BUY:  (settle_price - mid_price) * size
       - SELL: (mid_price - settle_price) * size

    2. execution_edge: profitto dall'esecuzione migliore del mid
       - BUY:  (mid_price - price) * size  (positivo = comprato sotto il mid)
       - SELL: (price - mid_price) * size  (positivo = venduto sopra il mid)

    Proprieta': total_pnl = directional_alpha + execution_edge

    Parametri:
        trades: lista di dizionari trade con chiavi price, size, side, mid_price
                settle_price opzionale (fallback: ultimo price)

    Ritorna:
        dict con total_pnl, directional_alpha, execution_edge, execution_pct
    """
    if not trades:
        return {
            "total_pnl": 0.0,
            "directional_alpha": 0.0,
            "execution_edge": 0.0,
            "execution_pct": 0.0,
        }

    last_price = trades[-1].get("price", 0.0)
    total_alpha = 0.0
    total_exec = 0.0

    for trade in trades:
        size = float(trade.get("size", 0.0))
        side = str(trade.get("side", "")).upper()
        price = float(trade.get("price", 0.0))
        mid = float(trade.get("mid_price", price))
        settle = float(trade.get("settle_price", 0.0))

        if settle == 0.0:
            settle = last_price

        if side == "BUY":
            alpha = (settle - mid) * size
            edge = (mid - price) * size
        elif side == "SELL":
            alpha = (mid - settle) * size
            edge = (price - mid) * size
        else:
            # Fallback: tratta come BUY
            alpha = (settle - mid) * size
            edge = (mid - price) * size

        total_alpha += alpha
        total_exec += edge

    total_pnl = total_alpha + total_exec

    # Percentuale del PnL totale dovuta all'execution edge
    if abs(total_pnl) > 1e-10:
        exec_pct = total_exec / total_pnl
    else:
        exec_pct = 0.0

    return {
        "total_pnl": round(total_pnl, 6),
        "directional_alpha": round(total_alpha, 6),
        "execution_edge": round(total_exec, 6),
        "execution_pct": round(exec_pct, 6),
    }


def sharpe_ratio(pnl_array: list | np.ndarray, periods_per_year: int = 365) -> float:
    """
    Sharpe ratio annualizzato.

    Formula: SR = (mean(pnl) / std(pnl)) * sqrt(periods_per_year)

    Parametri:
        pnl_array:        lista o array di PnL per-periodo
        periods_per_year:  periodi per anno per annualizzazione (default 365 = daily)

    Ritorna:
        float: Sharpe ratio annualizzato. 0.0 se std == 0 o array vuoto.
    """
    pnl = np.asarray(pnl_array, dtype=np.float64)

    if len(pnl) < 2:
        return 0.0

    std = np.std(pnl, ddof=1)
    if std < 1e-12:
        return 0.0

    mean = np.mean(pnl)
    return float(mean / std * np.sqrt(periods_per_year))


def max_drawdown(equity_curve: list | np.ndarray) -> tuple[float, float]:
    """
    Calcola il massimo drawdown da una equity curve (PnL cumulativo).

    Parametri:
        equity_curve: lista o array di valori cumulativi dell'equity

    Ritorna:
        tuple (max_dd_value, max_dd_pct):
            - max_dd_value: drawdown massimo in valore assoluto (sempre >= 0)
            - max_dd_pct:   drawdown massimo percentuale rispetto al picco (0-1)
    """
    eq = np.asarray(equity_curve, dtype=np.float64)

    if len(eq) < 2:
        return (0.0, 0.0)

    running_max = np.maximum.accumulate(eq)
    drawdowns = running_max - eq

    max_dd_val = float(np.max(drawdowns))

    # Percentuale rispetto al picco al momento del drawdown
    # Trova l'indice del massimo drawdown
    max_dd_idx = int(np.argmax(drawdowns))
    peak_at_dd = running_max[max_dd_idx]

    if abs(peak_at_dd) > 1e-12:
        max_dd_pct = max_dd_val / abs(peak_at_dd)
    else:
        max_dd_pct = 0.0

    return (round(max_dd_val, 6), round(max_dd_pct, 6))


# ── Classe principale ──


class WhaleBacktester:
    """
    Framework di backtest multi-scenario per whale trading.

    Combina scenario analysis, block bootstrap e decomposizione PnL
    per validare se la performance di un whale e' statisticamente
    significativa e quale parte del PnL proviene dall'alpha direzionale
    vs dall'abilita' di esecuzione.

    Uso:
        bt = WhaleBacktester(trades)
        summary = bt.summary()
        if bt.is_statistically_significant():
            print("Performance significativa!")
    """

    SCENARIOS = ("actual", "mid", "all_maker", "all_taker")

    def __init__(self, trades: list[dict]):
        """
        Inizializza il backtester con una lista di trade.

        Parametri:
            trades: lista di dizionari con chiavi:
                    price, size, side, settle_price (opt),
                    mid_price (opt), best_bid (opt), best_ask (opt)
        """
        self.trades = trades
        self._scenario_cache: dict[str, list[float]] = {}
        self._bootstrap_cache: dict[str, dict] = {}

    def run_all_scenarios(self) -> dict[str, dict]:
        """
        Esegue tutti e 4 gli scenari di esecuzione.

        Ritorna:
            dict {scenario_name: {pnl_list, total_pnl, sharpe, max_dd, max_dd_pct, n_trades}}
        """
        results = {}

        for sc in self.SCENARIOS:
            pnl_list = self._get_scenario_pnl(sc)
            pnl_arr = np.asarray(pnl_list, dtype=np.float64)

            total = float(np.sum(pnl_arr)) if len(pnl_arr) > 0 else 0.0
            sr = sharpe_ratio(pnl_arr)

            # Equity curve e max drawdown
            if len(pnl_arr) > 0:
                equity = np.cumsum(pnl_arr)
                dd_val, dd_pct = max_drawdown(equity)
            else:
                dd_val, dd_pct = 0.0, 0.0

            results[sc] = {
                "pnl_list": pnl_list,
                "total_pnl": round(total, 6),
                "sharpe": round(sr, 4),
                "max_dd": dd_val,
                "max_dd_pct": dd_pct,
                "n_trades": len(pnl_list),
            }

        return results

    def run_bootstrap(
        self,
        scenario: str = "actual",
        iters: int = 20000,
        block_len: int = 50,
        seed: int = 7,
    ) -> dict:
        """
        Esegue block bootstrap sullo scenario specificato.

        Parametri:
            scenario:  scenario di esecuzione (default "actual")
            iters:     iterazioni bootstrap (default 20000)
            block_len: lunghezza blocco circolare (default 50)
            seed:      seed riproducibilita' (default 7)

        Ritorna:
            dict con quantili per total_pnl e max_drawdown
        """
        cache_key = f"{scenario}_{iters}_{block_len}_{seed}"

        if cache_key in self._bootstrap_cache:
            return self._bootstrap_cache[cache_key]

        pnl_list = self._get_scenario_pnl(scenario)
        result = block_bootstrap(pnl_list, iters=iters, block_len=block_len, seed=seed)

        self._bootstrap_cache[cache_key] = result
        return result

    def is_statistically_significant(
        self,
        confidence: float = 0.95,
        scenario: str = "actual",
    ) -> bool:
        """
        Verifica se il PnL e' statisticamente significativo.

        Un whale ha performance significativa se il 5° percentile del PnL
        bootstrap e' > 0 (al livello di confidenza 95%).

        Per confidence=0.99 usa il 1° percentile.

        Parametri:
            confidence: livello di confidenza (default 0.95)
            scenario:   scenario su cui testare (default "actual")

        Ritorna:
            True se la performance e' significativa al livello richiesto
        """
        bootstrap = self.run_bootstrap(scenario=scenario)

        # Mappa confidenza -> quantile
        if confidence >= 0.99:
            quantile_key = "p01"
        elif confidence >= 0.95:
            quantile_key = "p05"
        else:
            quantile_key = "p05"

        lower_bound = bootstrap["total_pnl"].get(quantile_key, 0.0)
        return lower_bound > 0.0

    def summary(self) -> dict:
        """
        Report completo con tutti gli scenari, bootstrap e decomposizione.

        Ritorna:
            dict con:
                - scenarios: risultati per ogni scenario
                - bootstrap: quantili bootstrap per scenario "actual"
                - decomposition: alpha direzionale vs execution edge
                - is_significant_95: bool significativita' al 95%
                - is_significant_99: bool significativita' al 99%
                - n_trades: numero totale di trade
        """
        scenarios = self.run_all_scenarios()
        bootstrap = self.run_bootstrap(scenario="actual")
        decomp = pnl_decomposition(self.trades)

        return {
            "scenarios": {
                sc: {
                    "total_pnl": data["total_pnl"],
                    "sharpe": data["sharpe"],
                    "max_dd": data["max_dd"],
                    "max_dd_pct": data["max_dd_pct"],
                    "n_trades": data["n_trades"],
                }
                for sc, data in scenarios.items()
            },
            "bootstrap": bootstrap,
            "decomposition": decomp,
            "is_significant_95": self.is_statistically_significant(confidence=0.95),
            "is_significant_99": self.is_statistically_significant(confidence=0.99),
            "n_trades": len(self.trades),
        }

    def _get_scenario_pnl(self, scenario: str) -> list[float]:
        """Calcola e mette in cache il PnL per uno scenario."""
        if scenario not in self._scenario_cache:
            self._scenario_cache[scenario] = scenario_pnl(self.trades, scenario)
        return self._scenario_cache[scenario]


# ── Test con dati sintetici ──


def _generate_synthetic_trades(n: int = 200, seed: int = 42) -> list[dict]:
    """
    Genera trade sintetici per il test.

    Simula un whale con leggero edge positivo (~55% win rate)
    su mercati binari con prezzi tra 0.20 e 0.80.
    """
    rng = np.random.default_rng(seed)

    trades = []
    for i in range(n):
        # Prezzo mid random tra 0.20 e 0.80
        mid = rng.uniform(0.20, 0.80)

        # Spread tipico 1-3 centesimi
        half_spread = rng.uniform(0.005, 0.015)
        best_bid = mid - half_spread
        best_ask = mid + half_spread

        # Side: 60% BUY, 40% SELL
        side = "BUY" if rng.random() < 0.60 else "SELL"

        # Size tra $10 e $500
        size = float(rng.uniform(10, 500))

        # Prezzo esecuzione: tra bid e ask (maker/taker mix)
        execution_noise = rng.uniform(-half_spread * 0.5, half_spread * 0.5)
        price = mid + execution_noise

        # Settle price: whale ha leggero edge (~55% corretto)
        # Per BUY: settle > mid 55% delle volte
        # Per SELL: settle < mid 55% delle volte
        is_correct = rng.random() < 0.55
        settle_move = rng.uniform(0.05, 0.30)

        if side == "BUY":
            settle = mid + settle_move if is_correct else mid - settle_move
        else:
            settle = mid - settle_move if is_correct else mid + settle_move

        # Clamp settle tra 0 e 1 (binary market)
        settle = max(0.0, min(1.0, settle))

        trades.append({
            "price": round(price, 4),
            "size": round(size, 2),
            "side": side,
            "settle_price": round(settle, 4),
            "mid_price": round(mid, 4),
            "best_bid": round(best_bid, 4),
            "best_ask": round(best_ask, 4),
        })

    return trades


def _print_summary(summary: dict) -> None:
    """Stampa il report del backtest in formato tabellare."""
    print("\n" + "=" * 80)
    print("  WHALE BACKTEST — Report Multi-Scenario")
    print("=" * 80)

    # Scenari
    print(f"\n  {'Scenario':<14} {'PnL Totale':>12} {'Sharpe':>8} {'Max DD':>10} {'DD %':>8}")
    print("  " + "-" * 56)
    for sc, data in summary["scenarios"].items():
        print(
            f"  {sc:<14} {data['total_pnl']:>12.2f} {data['sharpe']:>8.2f} "
            f"{data['max_dd']:>10.2f} {data['max_dd_pct']:>7.1%}"
        )

    # Bootstrap
    print(f"\n  Bootstrap (scenario: actual, 20K iterazioni)")
    print("  " + "-" * 56)
    bs = summary["bootstrap"]
    for metric in ("total_pnl", "max_drawdown"):
        vals = bs[metric]
        print(
            f"  {metric:<16} "
            f"p01={vals['p01']:>10.2f}  p05={vals['p05']:>10.2f}  "
            f"p50={vals['p50']:>10.2f}  p95={vals['p95']:>10.2f}  "
            f"p99={vals['p99']:>10.2f}"
        )

    # Decomposizione
    print(f"\n  Decomposizione PnL")
    print("  " + "-" * 56)
    d = summary["decomposition"]
    print(f"  {'PnL Totale':<24} {d['total_pnl']:>12.2f}")
    print(f"  {'Directional Alpha':<24} {d['directional_alpha']:>12.2f}")
    print(f"  {'Execution Edge':<24} {d['execution_edge']:>12.2f}")
    print(f"  {'% da Execution':<24} {d['execution_pct']:>11.1%}")

    # Significativita'
    print(f"\n  Significativita' statistica")
    print("  " + "-" * 56)
    sig95 = "SI" if summary["is_significant_95"] else "NO"
    sig99 = "SI" if summary["is_significant_99"] else "NO"
    print(f"  {'Significativo al 95%':<24} {sig95}")
    print(f"  {'Significativo al 99%':<24} {sig99}")
    print(f"  {'N. trade analizzati':<24} {summary['n_trades']}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Whale Backtest — Test Monte Carlo multi-scenario"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Output dettagliato")
    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    )

    print("Generazione trade sintetici (n=200, seed=42)...")
    trades = _generate_synthetic_trades(n=200, seed=42)

    print(f"Trade generati: {len(trades)}")
    print(f"  Primo trade: {trades[0]}")
    print(f"  Ultimo trade: {trades[-1]}")

    # Esegui backtest
    bt = WhaleBacktester(trades)
    summary = bt.summary()

    _print_summary(summary)

    # Test aggiuntivi se verbose
    if args.verbose:
        print("\n--- Test funzioni standalone ---")

        # Sharpe ratio
        pnl_list = scenario_pnl(trades, "actual")
        sr = sharpe_ratio(pnl_list)
        print(f"Sharpe (actual): {sr:.4f}")

        # Max drawdown
        equity = list(np.cumsum(pnl_list))
        dd_val, dd_pct = max_drawdown(equity)
        print(f"Max DD: {dd_val:.2f} ({dd_pct:.1%})")

        # Bootstrap tutti gli scenari
        for sc in WhaleBacktester.SCENARIOS:
            bs = bt.run_bootstrap(scenario=sc)
            p50 = bs["total_pnl"]["p50"]
            p05 = bs["total_pnl"]["p05"]
            print(f"Bootstrap {sc:<12}: p50={p50:>10.2f}, p05={p05:>10.2f}")

    print("\nBacktest completato.")
