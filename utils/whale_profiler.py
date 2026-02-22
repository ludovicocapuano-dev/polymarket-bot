"""
Whale Profiler — Analisi comportamentale wallet per whitelist
=============================================================
v8.2: Profiler offline che analizza i wallet tracciati e produce una
whitelist JSON consumata da whale_copy.

Metriche (dall'articolo "How to Read the Mind of a Polymarket Whale"):
- Time Profitable: % del tempo in cui la posizione e' in profitto
- Accumulation Pattern: INCREASING (disciplinato) vs CHASING_HIGHER (FOMO)
- Hedge Check: % mercati con posizioni YES+NO (arbitraggio)
- Trading Intensity: frequenza trade, rilevamento bot

Uso standalone:
    python3 -m utils.whale_profiler           # profila e salva
    python3 -m utils.whale_profiler -v        # verbose
    python3 -m utils.whale_profiler --dry-run # solo stampa
"""

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Soglie ──
COMPOSITE_COPY_THRESHOLD = 0.65
COMPOSITE_WATCH_THRESHOLD = 0.45
TIME_PROF_COPY_MIN = 0.70
TIME_PROF_SKIP_MAX = 0.50

WHITELIST_PATH = "logs/whale_whitelist.json"


@dataclass
class MarketTrades:
    """Trade raggruppati per mercato."""
    market_id: str
    question: str
    trades: list[dict]  # [{side, price, size, timestamp}]
    # Calcolati post-init
    avg_yes_price: float = 0.0
    avg_no_price: float = 0.0
    yes_shares: float = 0.0
    no_shares: float = 0.0
    total_invested: float = 0.0

    def __post_init__(self):
        yes_cost = 0.0
        no_cost = 0.0
        for t in self.trades:
            side = t.get("side", "").upper()
            price = float(t.get("price", 0))
            size = float(t.get("size", 0))
            if side == "YES" or side == "BUY":
                yes_cost += size
                if price > 0:
                    self.yes_shares += size / price
            elif side == "NO" or side == "SELL":
                no_cost += size
                if price > 0:
                    self.no_shares += size / price

        self.total_invested = yes_cost + no_cost
        self.avg_yes_price = (yes_cost / self.yes_shares) if self.yes_shares > 0 else 0.0
        self.avg_no_price = (no_cost / self.no_shares) if self.no_shares > 0 else 0.0


@dataclass
class WalletMetrics:
    """Metriche comportamentali di un wallet whale."""
    address: str
    name: str
    time_profitable_pct: float = 0.0        # 0-1, target >0.80
    accumulation_pattern: str = "UNKNOWN"    # INCREASING / CHASING_HIGHER / MIXED / UNKNOWN
    accumulation_score: float = 0.0          # 0-1
    n_hedged_markets: int = 0                # mercati con YES+NO < $1.00
    hedge_ratio: float = 0.0                 # % mercati hedgiati
    avg_minutes_between_trades: float = 0.0
    is_likely_bot: bool = False
    total_markets_analyzed: int = 0
    total_trades_analyzed: int = 0
    data_quality: str = "INSUFFICIENT"       # INSUFFICIENT / LOW / MEDIUM / HIGH
    composite_score: float = 0.0             # 0-1
    recommendation: str = "SKIP"             # COPY / WATCH / SKIP
    profiled_at: float = field(default_factory=time.time)


@dataclass
class WhaleWhitelist:
    """Whitelist generata dal profiler."""
    generated_at: float
    wallets: dict[str, WalletMetrics]  # address -> metrics
    summary: dict = field(default_factory=dict)


class WhaleProfiler:
    """
    Profiler comportamentale per whale Polymarket.

    Analizza lo storico trade dei wallet tracciati e calcola metriche
    comportamentali per produrre una whitelist COPY/WATCH/SKIP.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"

    def profile_all_wallets(self) -> WhaleWhitelist:
        """Entry point: profila tutti i TRACKED_WALLETS."""
        from strategies.whale_copy import TRACKED_WALLETS

        wallets: dict[str, WalletMetrics] = {}
        n_copy = 0
        n_watch = 0
        n_skip = 0

        for name, info in TRACKED_WALLETS.items():
            address = info.get("address", "")
            if not address:
                continue

            try:
                metrics = self._profile_wallet(address, name)
                wallets[address] = metrics

                if metrics.recommendation == "COPY":
                    n_copy += 1
                elif metrics.recommendation == "WATCH":
                    n_watch += 1
                else:
                    n_skip += 1

                if self.verbose:
                    logger.info(
                        f"[WHALE_PROFILER] {name}: score={metrics.composite_score:.2f} "
                        f"rec={metrics.recommendation} "
                        f"time_prof={metrics.time_profitable_pct:.0%} "
                        f"pattern={metrics.accumulation_pattern} "
                        f"hedge={metrics.hedge_ratio:.0%} "
                        f"quality={metrics.data_quality}"
                    )

            except Exception as e:
                logger.warning(f"[WHALE_PROFILER] Errore profiling {name}: {e}")
                wallets[address] = WalletMetrics(
                    address=address,
                    name=name,
                    data_quality="INSUFFICIENT",
                    recommendation="SKIP",
                )
                n_skip += 1

        whitelist = WhaleWhitelist(
            generated_at=time.time(),
            wallets=wallets,
            summary={
                "total": len(wallets),
                "copy": n_copy,
                "watch": n_watch,
                "skip": n_skip,
            },
        )

        logger.info(
            f"[WHALE_PROFILER] Profiling completato: "
            f"{len(wallets)} wallet → "
            f"COPY={n_copy} WATCH={n_watch} SKIP={n_skip}"
        )

        return whitelist

    def _profile_wallet(self, address: str, name: str) -> WalletMetrics:
        """Profila un singolo wallet."""
        trades = self._fetch_full_trade_history(address)
        market_trades = self._group_by_market(trades)

        n_trades = len(trades)
        n_markets = len(market_trades)

        data_quality = self._assess_data_quality(n_trades, n_markets)

        if data_quality == "INSUFFICIENT":
            return WalletMetrics(
                address=address,
                name=name,
                total_markets_analyzed=n_markets,
                total_trades_analyzed=n_trades,
                data_quality="INSUFFICIENT",
                recommendation="SKIP",
            )

        # Calcola metriche
        time_prof = self._calc_time_profitable(trades)
        pattern, acc_score = self._calc_accumulation_pattern(market_trades)
        n_hedged, hedge_ratio = self._calc_hedge_check(market_trades)
        avg_min, is_bot = self._calc_trading_intensity(trades)

        metrics = WalletMetrics(
            address=address,
            name=name,
            time_profitable_pct=time_prof,
            accumulation_pattern=pattern,
            accumulation_score=acc_score,
            n_hedged_markets=n_hedged,
            hedge_ratio=hedge_ratio,
            avg_minutes_between_trades=avg_min,
            is_likely_bot=is_bot,
            total_markets_analyzed=n_markets,
            total_trades_analyzed=n_trades,
            data_quality=data_quality,
        )

        metrics.composite_score = self._compute_composite_score(metrics)
        metrics.recommendation = self._classify_recommendation(
            metrics.composite_score, metrics
        )

        return metrics

    def _fetch_full_trade_history(self, address: str) -> list[dict]:
        """
        Fetch storico trade completo via Gamma API.
        NON applica il filtro 120s di whale_copy — serve tutto lo storico.
        """
        trades = []
        try:
            resp = self._session.get(
                "https://gamma-api.polymarket.com/activity",
                params={"address": address, "limit": 200},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.debug(
                    f"[WHALE_PROFILER] API activity status={resp.status_code} "
                    f"per {address[:10]}..."
                )
                return []

            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", data.get("positions", []))

            for item in items:
                market_id = (
                    item.get("conditionId", "") or
                    item.get("market_id", "") or
                    item.get("marketId", "") or
                    item.get("condition_id", "")
                )
                if not market_id:
                    continue

                side = item.get("side", item.get("outcome", ""))
                if isinstance(side, str):
                    side = side.upper()
                    if side in ("BUY", "LONG"):
                        side = item.get("outcome", "YES").upper()
                    elif side in ("SELL", "SHORT"):
                        side = "SELL"

                price = float(item.get("price", item.get("avgPrice", 0)))
                size = float(item.get("size", item.get("usdcSize", item.get("value", 0))))

                # Timestamp
                ts_raw = item.get("timestamp", item.get("createdAt", item.get("created_at", "")))
                ts = self._parse_timestamp(ts_raw)

                question = item.get("question", item.get("title", ""))

                trades.append({
                    "market_id": market_id,
                    "side": side,
                    "price": price,
                    "size": size,
                    "timestamp": ts,
                    "question": question,
                })

        except Exception as e:
            logger.warning(f"[WHALE_PROFILER] Errore fetch history {address[:10]}...: {e}")

        return trades

    def _parse_timestamp(self, ts_raw) -> float:
        """Parsa timestamp in diversi formati."""
        if isinstance(ts_raw, (int, float)):
            ts = float(ts_raw)
            if ts > 1e12:
                ts /= 1000
            return ts
        elif isinstance(ts_raw, str) and ts_raw:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                return time.time()
        return time.time()

    def _group_by_market(self, trades: list[dict]) -> dict[str, MarketTrades]:
        """Raggruppa trade per market_id."""
        groups: dict[str, list[dict]] = {}
        questions: dict[str, str] = {}

        for t in trades:
            mid = t["market_id"]
            groups.setdefault(mid, []).append(t)
            if t.get("question"):
                questions[mid] = t["question"]

        result = {}
        for mid, trade_list in groups.items():
            result[mid] = MarketTrades(
                market_id=mid,
                question=questions.get(mid, ""),
                trades=trade_list,
            )
        return result

    def _calc_time_profitable(self, trades: list[dict]) -> float:
        """
        Calcola la % di tempo in cui le posizioni sono in profitto.

        Simula il P&L running: per ogni trade, traccia il costo medio
        e confronta con il prezzo corrente nel tempo.
        """
        if not trades:
            return 0.0

        # Ordina per timestamp
        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", 0))

        # Simula P&L tra trade consecutivi
        n_profitable_intervals = 0
        n_total_intervals = 0
        running_cost = 0.0
        running_value = 0.0

        for i, t in enumerate(sorted_trades):
            price = t.get("price", 0)
            size = t.get("size", 0)
            side = t.get("side", "").upper()

            if side in ("YES", "BUY"):
                running_cost += size
                if price > 0:
                    running_value += size  # al momento dell'acquisto, value = cost
            elif side == "SELL":
                # Vendita: realizzo parziale
                running_cost = max(0, running_cost - size)

            # Ogni intervallo tra trade: il whale era in profitto?
            if i > 0 and running_cost > 0:
                n_total_intervals += 1
                # Stima semplificata: se il prezzo corrente > prezzo medio di acquisto
                # il whale e' in profitto
                if price > 0 and running_value > 0:
                    avg_entry = running_cost / max(running_value / running_cost, 0.01) if running_cost > 0 else 1.0
                    if price <= avg_entry * 1.05:  # margine 5%
                        n_profitable_intervals += 1

        if n_total_intervals == 0:
            # Fallback: stima basata su buy vs sell ratio
            buys = sum(1 for t in trades if t.get("side", "").upper() in ("YES", "BUY"))
            sells = sum(1 for t in trades if t.get("side", "").upper() == "SELL")
            if buys + sells > 0:
                return min(buys / (buys + sells), 0.95)
            return 0.5

        return n_profitable_intervals / n_total_intervals

    def _calc_accumulation_pattern(
        self, market_trades: dict[str, MarketTrades]
    ) -> tuple[str, float]:
        """
        Analizza il pattern di accumulo per mercato.

        INCREASING: compra a prezzi progressivamente migliori (disciplinato)
        CHASING_HIGHER: compra a prezzi crescenti (FOMO)
        MIXED: pattern misto
        UNKNOWN: dati insufficienti
        """
        if not market_trades:
            return "UNKNOWN", 0.0

        n_increasing = 0
        n_chasing = 0
        n_analyzed = 0

        for mid, mt in market_trades.items():
            buy_trades = [
                t for t in mt.trades
                if t.get("side", "").upper() in ("YES", "BUY") and t.get("price", 0) > 0
            ]

            if len(buy_trades) < 2:
                continue

            # Ordina per timestamp
            buy_trades.sort(key=lambda t: t.get("timestamp", 0))
            prices = [t["price"] for t in buy_trades]

            # Conta trend: prezzo medio della seconda meta' vs prima meta'
            mid_idx = len(prices) // 2
            first_half_avg = sum(prices[:mid_idx]) / mid_idx if mid_idx > 0 else 0
            second_half_avg = sum(prices[mid_idx:]) / len(prices[mid_idx:]) if len(prices[mid_idx:]) > 0 else 0

            n_analyzed += 1
            if first_half_avg > 0:
                if second_half_avg <= first_half_avg * 1.02:
                    # Compra a prezzi stabili o decrescenti = disciplinato
                    n_increasing += 1
                else:
                    # Compra a prezzi crescenti = FOMO
                    n_chasing += 1

        if n_analyzed == 0:
            return "UNKNOWN", 0.0

        increasing_ratio = n_increasing / n_analyzed
        chasing_ratio = n_chasing / n_analyzed

        if increasing_ratio >= 0.60:
            pattern = "INCREASING"
            score = min(increasing_ratio, 1.0)
        elif chasing_ratio >= 0.60:
            pattern = "CHASING_HIGHER"
            score = max(1.0 - chasing_ratio, 0.1)
        else:
            pattern = "MIXED"
            score = 0.5

        return pattern, score

    def _calc_hedge_check(
        self, market_trades: dict[str, MarketTrades]
    ) -> tuple[int, float]:
        """
        Conta mercati dove il whale ha posizioni YES + NO (hedge/arbitraggio).
        YES+NO < $1.00 = probabile arbitraggio (sofisticazione).
        """
        if not market_trades:
            return 0, 0.0

        n_hedged = 0
        n_with_positions = 0

        for mid, mt in market_trades.items():
            if mt.yes_shares > 0 or mt.no_shares > 0:
                n_with_positions += 1

            if mt.yes_shares > 0 and mt.no_shares > 0:
                # Ha entrambe le posizioni — potenziale hedge
                total_cost_per_share = mt.avg_yes_price + mt.avg_no_price
                if total_cost_per_share < 1.00 and total_cost_per_share > 0:
                    n_hedged += 1

        hedge_ratio = n_hedged / n_with_positions if n_with_positions > 0 else 0.0
        return n_hedged, hedge_ratio

    def _calc_trading_intensity(self, trades: list[dict]) -> tuple[float, bool]:
        """
        Calcola intervallo medio tra trade e rileva comportamento bot.

        Returns: (avg_minutes_between_trades, is_likely_bot)
        """
        if len(trades) < 2:
            return 0.0, False

        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", 0))
        intervals = []

        for i in range(1, len(sorted_trades)):
            ts1 = sorted_trades[i - 1].get("timestamp", 0)
            ts2 = sorted_trades[i].get("timestamp", 0)
            if ts1 > 0 and ts2 > ts1:
                intervals.append((ts2 - ts1) / 60.0)  # in minuti

        if not intervals:
            return 0.0, False

        avg_min = sum(intervals) / len(intervals)

        # Bot detection: intervalli troppo regolari o troppo rapidi
        is_bot = False
        if avg_min < 1.0:
            # Meno di 1 minuto in media = probabile bot
            is_bot = True
        elif len(intervals) >= 5:
            # Deviazione standard molto bassa = pattern meccanico
            mean = avg_min
            variance = sum((x - mean) ** 2 for x in intervals) / len(intervals)
            std = variance ** 0.5
            cv = std / mean if mean > 0 else 0  # coefficiente di variazione
            if cv < 0.10 and avg_min < 5.0:
                is_bot = True

        return avg_min, is_bot

    def _compute_composite_score(self, metrics: WalletMetrics) -> float:
        """
        Score composito pesato.

        score = (
            time_profitable_pct * 0.35 +
            accumulation_score  * 0.30 +
            hedge_bonus         * 0.20 +
            intensity_score     * 0.15
        ) × data_quality_penalty
        """
        # Hedge bonus: 0.3 neutro, 1.0 se hedgia >10%
        if metrics.hedge_ratio > 0.10:
            hedge_bonus = 1.0
        elif metrics.hedge_ratio > 0.05:
            hedge_bonus = 0.7
        else:
            hedge_bonus = 0.3

        # Intensity score: 0.6 per bot, 1.0 per sweet spot (5-60 min)
        if metrics.is_likely_bot:
            intensity_score = 0.6
        elif 5.0 <= metrics.avg_minutes_between_trades <= 60.0:
            intensity_score = 1.0
        elif 1.0 <= metrics.avg_minutes_between_trades < 5.0:
            intensity_score = 0.8
        elif metrics.avg_minutes_between_trades > 60.0:
            intensity_score = 0.7
        else:
            intensity_score = 0.5

        raw_score = (
            metrics.time_profitable_pct * 0.35 +
            metrics.accumulation_score * 0.30 +
            hedge_bonus * 0.20 +
            intensity_score * 0.15
        )

        # Data quality penalty
        penalty = {
            "HIGH": 1.0,
            "MEDIUM": 0.9,
            "LOW": 0.7,
            "INSUFFICIENT": 0.0,
        }.get(metrics.data_quality, 0.0)

        return raw_score * penalty

    def _classify_recommendation(
        self, score: float, metrics: WalletMetrics
    ) -> str:
        """Classifica in COPY / WATCH / SKIP."""
        # SKIP conditions
        if score < COMPOSITE_WATCH_THRESHOLD:
            return "SKIP"
        if metrics.data_quality == "INSUFFICIENT":
            return "SKIP"
        if metrics.time_profitable_pct < TIME_PROF_SKIP_MAX:
            return "SKIP"

        # COPY conditions
        if (
            score >= COMPOSITE_COPY_THRESHOLD
            and metrics.time_profitable_pct >= TIME_PROF_COPY_MIN
            and metrics.accumulation_pattern != "CHASING_HIGHER"
            and metrics.data_quality in ("MEDIUM", "HIGH")
        ):
            return "COPY"

        # Everything else = WATCH
        return "WATCH"

    def _assess_data_quality(self, n_trades: int, n_markets: int) -> str:
        """Valuta qualita' dati in base a quantita'."""
        if n_trades >= 100 and n_markets >= 20:
            return "HIGH"
        if n_trades >= 50 and n_markets >= 10:
            return "MEDIUM"
        if n_trades >= 20 and n_markets >= 5:
            return "LOW"
        return "INSUFFICIENT"

    def save_whitelist(self, wl: WhaleWhitelist) -> None:
        """Salva whitelist in logs/whale_whitelist.json."""
        os.makedirs("logs", exist_ok=True)

        data = {
            "generated_at": wl.generated_at,
            "summary": wl.summary,
            "wallets": {},
        }

        for addr, m in wl.wallets.items():
            data["wallets"][addr] = {
                "address": m.address,
                "name": m.name,
                "composite_score": round(m.composite_score, 4),
                "recommendation": m.recommendation,
                "time_profitable_pct": round(m.time_profitable_pct, 4),
                "accumulation_pattern": m.accumulation_pattern,
                "accumulation_score": round(m.accumulation_score, 4),
                "n_hedged_markets": m.n_hedged_markets,
                "hedge_ratio": round(m.hedge_ratio, 4),
                "avg_minutes_between_trades": round(m.avg_minutes_between_trades, 2),
                "is_likely_bot": m.is_likely_bot,
                "total_markets_analyzed": m.total_markets_analyzed,
                "total_trades_analyzed": m.total_trades_analyzed,
                "data_quality": m.data_quality,
                "profiled_at": m.profiled_at,
            }

        with open(WHITELIST_PATH, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"[WHALE_PROFILER] Whitelist salvata in {WHITELIST_PATH}")

    @staticmethod
    def load_whitelist(path: str = WHITELIST_PATH) -> Optional[dict]:
        """
        Carica whitelist da JSON. Usato da whale_copy.

        Returns dict {address: {score, recommendation, ...}} o None se non esiste.
        """
        try:
            if not os.path.exists(path):
                return None

            with open(path) as f:
                data = json.load(f)

            return data.get("wallets", {})
        except Exception as e:
            logger.debug(f"[WHALE_PROFILER] Errore caricamento whitelist: {e}")
            return None


def _print_report(wl: WhaleWhitelist) -> None:
    """Stampa report tabellare."""
    print("\n" + "=" * 90)
    print("  WHALE PROFILER — Report Analisi Comportamentale")
    print("=" * 90)
    print(
        f"  {'Nome':<16} {'Score':>6} {'Rec':<6} {'TimProf':>7} "
        f"{'Pattern':<16} {'Hedge':>6} {'Bot':>4} {'Quality':<8} {'Trades':>6}"
    )
    print("-" * 90)

    for addr, m in sorted(wl.wallets.items(), key=lambda x: x[1].composite_score, reverse=True):
        bot_str = "Yes" if m.is_likely_bot else "No"
        print(
            f"  {m.name:<16} {m.composite_score:>6.2f} {m.recommendation:<6} "
            f"{m.time_profitable_pct:>6.0%} "
            f"{m.accumulation_pattern:<16} {m.hedge_ratio:>5.0%} "
            f"{bot_str:>4} {m.data_quality:<8} {m.total_trades_analyzed:>6}"
        )

    print("-" * 90)
    s = wl.summary
    print(
        f"  Totale: {s.get('total', 0)} wallet | "
        f"COPY: {s.get('copy', 0)} | "
        f"WATCH: {s.get('watch', 0)} | "
        f"SKIP: {s.get('skip', 0)}"
    )
    print("=" * 90 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Whale Profiler — Analisi comportamentale wallet")
    parser.add_argument("-v", "--verbose", action="store_true", help="Output dettagliato")
    parser.add_argument("--dry-run", action="store_true", help="Solo stampa, non salvare")
    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    )

    profiler = WhaleProfiler(verbose=args.verbose)
    whitelist = profiler.profile_all_wallets()

    _print_report(whitelist)

    if not args.dry_run:
        profiler.save_whitelist(whitelist)
        print(f"Whitelist salvata in {WHITELIST_PATH}")
    else:
        print("[DRY-RUN] Whitelist NON salvata")


if __name__ == "__main__":
    main()
