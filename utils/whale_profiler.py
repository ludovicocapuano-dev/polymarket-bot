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

# ── Data API Pagination ──
DATA_API_PAGE_SIZE = 200
DATA_API_MAX_OFFSET = 1000     # hard cap Data API (oltre non ritorna dati)
DATA_API_MAX_PAGES = 6         # safety: max 6 pagine (1200 record)

# ── Goldsky Subgraph ──
GOLDSKY_ENDPOINT = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn"
GOLDSKY_PAGE_SIZE = 1000
GOLDSKY_REQUEST_INTERVAL = 1.0  # secondi tra richieste
GOLDSKY_CACHE_TTL = 3600 * 6    # 6 ore
GOLDSKY_MAX_PAGES = 50          # safety: max 50K events


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
    # v10.4: Metriche avanzate (ispirate da polybot)
    execution_quality: float = 0.0           # avg_price / avg_mid — <1.0 = buona esecuzione
    maker_ratio: float = 0.0                 # % trade eseguiti come maker (sotto mid)
    complete_set_ratio: float = 0.0          # % trade parte di complete set (entro 60s)
    complete_set_avg_edge: float = 0.0       # edge medio sui complete set: 1-(p_yes+p_no)
    complete_set_avg_pairing_s: float = 0.0  # tempo medio tra le due gambe (secondi)
    trader_type: str = "UNKNOWN"             # DIRECTIONAL / ARBITRAGEUR / MIXED / UNKNOWN


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
        # Goldsky state
        self._goldsky_cache: dict[str, tuple[list[dict], float]] = {}
        self._goldsky_last_request_at: float = 0.0

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

        # Calcola metriche base
        time_prof = self._calc_time_profitable(trades)
        pattern, acc_score = self._calc_accumulation_pattern(market_trades)
        n_hedged, hedge_ratio = self._calc_hedge_check(market_trades)
        avg_min, is_bot = self._calc_trading_intensity(trades)

        # Calcola metriche avanzate (v10.4)
        exec_quality = self._calc_execution_quality(trades)
        maker_ratio = self._calc_maker_ratio(trades)
        cs_ratio, cs_edge, cs_pairing, trader_type = self._calc_complete_set_detection(trades)

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
            execution_quality=exec_quality,
            maker_ratio=maker_ratio,
            complete_set_ratio=cs_ratio,
            complete_set_avg_edge=cs_edge,
            complete_set_avg_pairing_s=cs_pairing,
            trader_type=trader_type,
        )

        metrics.composite_score = self._compute_composite_score(metrics)
        metrics.recommendation = self._classify_recommendation(
            metrics.composite_score, metrics
        )

        return metrics

    def _fetch_full_trade_history(self, address: str) -> list[dict]:
        """
        Fetch storico trade completo via Data API con paginazione.
        v10.4: paginazione offset (cap 1000) + Goldsky supplementation.
        v10.2: migrato da gamma-api (404 dal 2026) a data-api.polymarket.com.
        NON applica il filtro 120s di whale_copy — serve tutto lo storico.
        """
        trades = []
        total_raw_items = 0
        last_page_full = False
        prev_page_sig = None

        try:
            for page in range(DATA_API_MAX_PAGES):
                offset = page * DATA_API_PAGE_SIZE
                if offset > DATA_API_MAX_OFFSET:
                    break

                resp = self._session.get(
                    "https://data-api.polymarket.com/activity",
                    params={
                        "user": address,
                        "limit": DATA_API_PAGE_SIZE,
                        "offset": offset,
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.debug(
                        f"[WHALE_PROFILER] data-api HTTP {resp.status_code} "
                        f"per {address[:10]}... offset={offset}"
                    )
                    break

                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", data.get("positions", []))
                page_count = len(items)
                total_raw_items += page_count

                if page_count == 0:
                    break

                # Page signature per rilevare stallo (Data API a volte
                # ritorna la stessa pagina con offset diversi)
                page_sig = (
                    page_count,
                    items[0].get("transactionHash", ""),
                    items[-1].get("transactionHash", ""),
                )
                if page_sig == prev_page_sig:
                    logger.debug(
                        f"[WHALE_PROFILER] Pagina stabile a offset={offset}, "
                        f"stop paginazione per {address[:10]}..."
                    )
                    break
                prev_page_sig = page_sig

                for item in items:
                    trade = self._parse_data_api_item(item)
                    if trade is not None:
                        trades.append(trade)

                last_page_full = (page_count >= DATA_API_PAGE_SIZE)
                if not last_page_full:
                    break  # Ultima pagina (non piena)

        except Exception as e:
            logger.warning(f"[WHALE_PROFILER] Errore fetch history {address[:10]}...: {e}")

        if total_raw_items > DATA_API_PAGE_SIZE:
            logger.info(
                f"[WHALE_PROFILER] Paginazione Data API: {total_raw_items} raw items, "
                f"{len(trades)} trade parsed per {address[:10]}..."
            )

        # Goldsky supplementation: se l'ultima pagina Data API era piena,
        # i dati potrebbero essere troncati → fetch Goldsky per completare
        if last_page_full:
            logger.info(
                f"[GOLDSKY] Data API hit pagination limit ({total_raw_items} items) "
                f"for {address[:10]}... — fetching full history from subgraph"
            )
            try:
                gs_trades = self._fetch_goldsky_history_cached(address)
                if gs_trades:
                    # Deduplica: preferisci tx_hash se disponibile, fallback
                    # a (timestamp, market_id, side)
                    existing_keys = set()
                    for t in trades:
                        key = self._trade_dedup_key(t)
                        existing_keys.add(key)

                    merged = 0
                    for gt in gs_trades:
                        key = self._trade_dedup_key(gt)
                        if key not in existing_keys:
                            trades.append(gt)
                            existing_keys.add(key)
                            merged += 1

                    logger.info(
                        f"[GOLDSKY] Merged {merged} additional trades "
                        f"(total: {len(trades)}) for {address[:10]}..."
                    )
            except Exception as e:
                logger.warning(f"[GOLDSKY] Errore supplementation {address[:10]}...: {e}")

        return trades

    def _parse_data_api_item(self, item: dict) -> dict | None:
        """Parsa un singolo item dalla Data API in formato trade standard."""
        # Filtra solo BUY/TRADE (skip REDEEM, SELL per profiling)
        trade_type = item.get("type", "").upper()
        if trade_type in ("REDEEM",):
            return None

        market_id = (
            item.get("conditionId", "") or
            item.get("market_id", "") or
            item.get("marketId", "") or
            item.get("condition_id", "")
        )
        if not market_id:
            return None

        # Side: data-api usa outcomeIndex (0=YES, 1=NO) e side ("BUY"/"SELL")
        side = item.get("side", item.get("outcome", ""))
        if isinstance(side, str):
            side = side.upper()
            if side in ("BUY", "LONG", ""):
                idx = item.get("outcomeIndex", item.get("outcome_index", -1))
                if idx == 0:
                    side = "YES"
                elif idx == 1:
                    side = "NO"
                elif side in ("YES", "NO"):
                    pass
                else:
                    side = item.get("outcome", "YES").upper()
            elif side in ("SELL", "SHORT"):
                side = "SELL"

        price = float(item.get("price", item.get("avgPrice", 0)))
        size = float(item.get("usdcSize", item.get("size", item.get("value", 0))))

        # Timestamp (data-api usa unix timestamp in secondi)
        ts_raw = item.get("timestamp", item.get("createdAt", item.get("created_at", "")))
        ts = self._parse_timestamp(ts_raw)

        question = item.get("question", item.get("title", ""))
        tx_hash = item.get("transactionHash", item.get("transaction_hash", ""))
        token_id = item.get("asset", item.get("tokenId", item.get("token_id", "")))

        return {
            "market_id": market_id,
            "side": side,
            "price": price,
            "size": size,
            "timestamp": ts,
            "question": question,
            "transaction_hash": tx_hash,
            "token_id": token_id,
        }

    @staticmethod
    def _trade_dedup_key(trade: dict) -> tuple:
        """
        Chiave di deduplicazione per merge Data API + Goldsky.
        Usa tx_hash + market_id + side se tx_hash disponibile,
        altrimenti fallback a (timestamp, market_id, side, price).
        """
        tx = trade.get("transaction_hash", "")
        if tx:
            return (tx, trade.get("market_id", ""), trade.get("side", ""))
        return (
            round(trade.get("timestamp", 0), 0),
            trade.get("market_id", ""),
            trade.get("side", ""),
            round(trade.get("price", 0), 3),
        )

    # ── Goldsky Subgraph Methods ──

    def _fetch_goldsky_history_cached(self, address: str) -> list[dict]:
        """
        Fetch storico trade da Goldsky con cache in-memory + file.
        Cache TTL: 6 ore.
        """
        addr_lower = address.lower()
        now = time.time()

        # 1. In-memory cache
        if addr_lower in self._goldsky_cache:
            cached_trades, cached_at = self._goldsky_cache[addr_lower]
            if now - cached_at < GOLDSKY_CACHE_TTL:
                logger.debug(f"[GOLDSKY] Cache hit (memory) for {addr_lower[:10]}...")
                return cached_trades

        # 2. File cache
        cache_dir = "logs/goldsky_cache"
        cache_file = os.path.join(cache_dir, f"{addr_lower}.json")
        try:
            if os.path.exists(cache_file):
                mtime = os.path.getmtime(cache_file)
                if now - mtime < GOLDSKY_CACHE_TTL:
                    with open(cache_file) as f:
                        cached_trades = json.load(f)
                    self._goldsky_cache[addr_lower] = (cached_trades, mtime)
                    logger.debug(f"[GOLDSKY] Cache hit (file) for {addr_lower[:10]}...")
                    return cached_trades
        except Exception:
            pass  # Cache corrotta → fetch fresco

        # 3. Fetch fresco
        trades = self._fetch_goldsky_history(addr_lower)

        # Salva in cache
        self._goldsky_cache[addr_lower] = (trades, now)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(trades, f)
        except Exception as e:
            logger.debug(f"[GOLDSKY] Errore salvataggio cache file: {e}")

        return trades

    def _fetch_goldsky_history(self, address: str) -> list[dict]:
        """
        Fetch storico completo da Goldsky subgraph (orderFilledEvents).
        Due passate: maker + taker. Paginazione via timestamp_gt + id_gt.
        """
        all_trades: list[dict] = []
        seen_ids: set[str] = set()

        for role in ("maker", "taker"):
            timestamp_gt = "0"
            id_gt = ""
            for page in range(GOLDSKY_MAX_PAGES):
                # Rate limiting
                elapsed = time.time() - self._goldsky_last_request_at
                if elapsed < GOLDSKY_REQUEST_INTERVAL:
                    time.sleep(GOLDSKY_REQUEST_INTERVAL - elapsed)

                query = self._goldsky_query(address, role, timestamp_gt, id_gt)

                try:
                    self._goldsky_last_request_at = time.time()
                    resp = self._session.post(
                        GOLDSKY_ENDPOINT,
                        json={"query": query},
                        timeout=15,
                    )
                    if resp.status_code != 200:
                        logger.warning(
                            f"[GOLDSKY] HTTP {resp.status_code} for "
                            f"{address[:10]}... role={role} page={page}"
                        )
                        break

                    data = resp.json()
                    events = data.get("data", {}).get("orderFilledEvents", [])

                    if not events:
                        break

                    for event in events:
                        eid = event.get("id", "")
                        if eid in seen_ids:
                            continue
                        seen_ids.add(eid)

                        normalized = self._normalize_goldsky_event(event, address)
                        if normalized is not None:
                            all_trades.append(normalized)

                    # Paginazione: ultimo evento come cursor
                    last = events[-1]
                    timestamp_gt = str(last.get("timestamp", "0"))
                    id_gt = last.get("id", "")

                    if len(events) < GOLDSKY_PAGE_SIZE:
                        break  # Ultima pagina

                except requests.exceptions.Timeout:
                    logger.warning(
                        f"[GOLDSKY] Timeout page {page} for "
                        f"{address[:10]}... role={role}"
                    )
                    break
                except Exception as e:
                    logger.warning(
                        f"[GOLDSKY] Errore page {page} for "
                        f"{address[:10]}... role={role}: {e}"
                    )
                    break

        logger.info(
            f"[GOLDSKY] Fetched {len(all_trades)} trades for {address[:10]}..."
        )
        return all_trades

    def _goldsky_query(
        self, address: str, role: str, timestamp_gt: str, id_gt: str
    ) -> str:
        """Costruisce query GraphQL per orderFilledEvents."""
        where_clause = f'{role}: "{address}"'
        if timestamp_gt and timestamp_gt != "0":
            where_clause += f', timestamp_gt: "{timestamp_gt}"'
        if id_gt:
            where_clause += f', id_gt: "{id_gt}"'

        return (
            "{\n"
            f"  orderFilledEvents(\n"
            f"    first: {GOLDSKY_PAGE_SIZE}\n"
            f"    orderBy: timestamp\n"
            f"    orderDirection: asc\n"
            f"    where: {{{where_clause}}}\n"
            f"  ) {{\n"
            f"    id\n"
            f"    maker\n"
            f"    taker\n"
            f"    makerAssetId\n"
            f"    takerAssetId\n"
            f"    makerAmountFilled\n"
            f"    takerAmountFilled\n"
            f"    timestamp\n"
            f"    transactionHash\n"
            f"  }}\n"
            "}"
        )

    def _normalize_goldsky_event(
        self, event: dict, wallet_address: str
    ) -> dict | None:
        """
        Normalizza un orderFilledEvent Goldsky in formato trade standard.

        Identifica il lato USDC (assetId == "0" nel CTF subgraph) per
        determinare direzione e prezzo. Skip token-for-token swap.
        """
        try:
            maker = (event.get("maker") or "").lower()
            taker = (event.get("taker") or "").lower()
            maker_asset = event.get("makerAssetId", "")
            taker_asset = event.get("takerAssetId", "")
            maker_amount = float(event.get("makerAmountFilled", 0))
            taker_amount = float(event.get("takerAmountFilled", 0))
            timestamp = event.get("timestamp", "")

            # Identifica lato USDC: assetId "0" = USDC nel CTF subgraph
            wallet = wallet_address.lower()

            if maker_asset == "0":
                usdc_amount = maker_amount / 1e6
                token_amount = taker_amount / 1e6
                token_asset_id = taker_asset
                # Maker dà USDC → chi è il wallet?
                if wallet == maker:
                    side = "BUY"  # wallet paga USDC = compra token
                else:
                    side = "SELL"  # wallet riceve token, controparte paga USDC
            elif taker_asset == "0":
                usdc_amount = taker_amount / 1e6
                token_amount = maker_amount / 1e6
                token_asset_id = maker_asset
                # Taker dà USDC
                if wallet == taker:
                    side = "BUY"  # wallet paga USDC = compra token
                else:
                    side = "SELL"  # wallet riceve USDC = vende token
            else:
                # Nessun lato USDC — token-for-token swap, skip
                return None

            if token_amount <= 0 or usdc_amount <= 0:
                return None

            price = usdc_amount / token_amount

            # Sanity check prezzo
            if price < 0.001 or price > 1.5:
                return None

            ts = float(timestamp) if timestamp else time.time()
            tx_hash = event.get("transactionHash", "")

            return {
                "market_id": f"gs_{token_asset_id}",
                "side": side,
                "price": round(price, 4),
                "size": round(usdc_amount, 2),
                "timestamp": ts,
                "question": "",
                "transaction_hash": tx_hash,
                "token_id": token_asset_id,
            }

        except Exception:
            return None

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

    # ── Metriche Avanzate v10.4 (ispirate da polybot) ──

    def _calc_execution_quality(self, trades: list[dict]) -> float:
        """
        Calcola la qualita' di esecuzione: quanto il whale paga rispetto
        al mid-price stimato. < 1.0 = buona esecuzione (paga meno del mid).

        Stima mid come prezzo medio per mercato (proxy in assenza di TOB).
        """
        if not trades:
            return 0.0

        # Calcola prezzo medio per mercato come proxy del mid
        market_prices: dict[str, list[float]] = {}
        for t in trades:
            p = t.get("price", 0)
            if p > 0:
                market_prices.setdefault(t["market_id"], []).append(p)

        market_mid: dict[str, float] = {}
        for mid, prices in market_prices.items():
            market_mid[mid] = sum(prices) / len(prices)

        # Calcola rapporto prezzo effettivo / mid per i BUY
        ratios = []
        for t in trades:
            side = t.get("side", "").upper()
            price = t.get("price", 0)
            mid = market_mid.get(t["market_id"], 0)
            if side in ("YES", "BUY") and price > 0 and mid > 0:
                ratios.append(price / mid)

        if not ratios:
            return 0.0

        return sum(ratios) / len(ratios)

    def _calc_maker_ratio(self, trades: list[dict]) -> float:
        """
        Stima la % di trade eseguiti come maker (prezzo <= mid del mercato).

        Un whale che opera prevalentemente sotto il mid (maker) e' piu'
        sofisticato — exec type classification ispirata da polybot.
        """
        if not trades:
            return 0.0

        # Prezzo medio per mercato come proxy del mid
        market_prices: dict[str, list[float]] = {}
        for t in trades:
            p = t.get("price", 0)
            if p > 0:
                market_prices.setdefault(t["market_id"], []).append(p)

        market_mid: dict[str, float] = {}
        for mid, prices in market_prices.items():
            market_mid[mid] = sum(prices) / len(prices)

        n_maker = 0
        n_total = 0
        for t in trades:
            price = t.get("price", 0)
            mid = market_mid.get(t["market_id"], 0)
            side = t.get("side", "").upper()
            if price <= 0 or mid <= 0:
                continue
            n_total += 1
            # BUY sotto il mid = maker, SELL sopra il mid = maker
            if side in ("YES", "BUY") and price <= mid:
                n_maker += 1
            elif side in ("NO", "SELL") and price >= mid:
                n_maker += 1

        return n_maker / n_total if n_total > 0 else 0.0

    def _calc_complete_set_detection(
        self, trades: list[dict]
    ) -> tuple[float, float, float, str]:
        """
        Rileva complete sets: coppie di BUY su outcome opposti (YES+NO)
        sullo stesso mercato entro 60 secondi.

        Returns: (complete_set_ratio, avg_edge, avg_pairing_seconds, trader_type)
        - complete_set_ratio: % trade che fanno parte di un complete set
        - avg_edge: edge medio (1 - price_yes - price_no), positivo = arbitraggio
        - avg_pairing_seconds: tempo medio tra le due gambe
        - trader_type: ARBITRAGEUR (>40% cs) / DIRECTIONAL (<10%) / MIXED
        """
        if len(trades) < 2:
            return 0.0, 0.0, 0.0, "UNKNOWN"

        # Raggruppa BUY per mercato
        market_buys: dict[str, list[dict]] = {}
        for t in trades:
            side = t.get("side", "").upper()
            if side in ("YES", "BUY", "NO"):
                market_buys.setdefault(t["market_id"], []).append(t)

        paired_count = 0
        edges = []
        pairing_times = []
        used_indices: set[tuple[str, int]] = set()

        for mid, buys in market_buys.items():
            if len(buys) < 2:
                continue

            sorted_buys = sorted(buys, key=lambda t: t.get("timestamp", 0))

            # Per ogni trade, cerca il trade con outcome opposto piu' vicino
            for i, t1 in enumerate(sorted_buys):
                if (mid, i) in used_indices:
                    continue
                side1 = t1.get("side", "").upper()

                for j in range(i + 1, len(sorted_buys)):
                    if (mid, j) in used_indices:
                        continue
                    t2 = sorted_buys[j]
                    side2 = t2.get("side", "").upper()

                    # Devono essere outcome opposti
                    if not self._are_opposite_sides(side1, side2):
                        continue

                    delta_s = abs(
                        t2.get("timestamp", 0) - t1.get("timestamp", 0)
                    )
                    if delta_s > 60:
                        break  # Ordinati, se >60s non vale la pena continuare

                    # Match! Complete set trovato
                    used_indices.add((mid, i))
                    used_indices.add((mid, j))
                    paired_count += 2

                    p1 = t1.get("price", 0)
                    p2 = t2.get("price", 0)
                    if p1 > 0 and p2 > 0:
                        edge = 1.0 - p1 - p2
                        edges.append(edge)
                    pairing_times.append(delta_s)
                    break

        total_buy_trades = sum(len(v) for v in market_buys.values())
        cs_ratio = paired_count / total_buy_trades if total_buy_trades > 0 else 0.0
        avg_edge = sum(edges) / len(edges) if edges else 0.0
        avg_pairing = sum(pairing_times) / len(pairing_times) if pairing_times else 0.0

        # Classificazione trader
        if cs_ratio >= 0.40:
            trader_type = "ARBITRAGEUR"
        elif cs_ratio < 0.10:
            trader_type = "DIRECTIONAL"
        else:
            trader_type = "MIXED"

        return cs_ratio, avg_edge, avg_pairing, trader_type

    @staticmethod
    def _are_opposite_sides(side1: str, side2: str) -> bool:
        """Verifica se due side sono opposti (YES/NO o BUY/SELL)."""
        opposites = {
            ("YES", "NO"), ("NO", "YES"),
            ("BUY", "SELL"), ("SELL", "BUY"),
            ("YES", "SELL"), ("SELL", "YES"),
            ("BUY", "NO"), ("NO", "BUY"),
        }
        return (side1, side2) in opposites

    def _compute_composite_score(self, metrics: WalletMetrics) -> float:
        """
        Score composito pesato.

        v10.4: aggiunto maker_ratio_bonus (20%), ribilanciati pesi.
        score = (
            time_profitable_pct * 0.30 +
            accumulation_score  * 0.25 +
            maker_ratio_bonus   * 0.20 +
            hedge_bonus         * 0.10 +
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

        # Maker ratio bonus (v10.4): whale che operano come maker sono
        # piu' sofisticati — exec type classification ispirata da polybot
        if metrics.maker_ratio >= 0.60:
            maker_bonus = 1.0
        elif metrics.maker_ratio >= 0.40:
            maker_bonus = 0.7
        else:
            maker_bonus = 0.3

        raw_score = (
            metrics.time_profitable_pct * 0.30 +
            metrics.accumulation_score * 0.25 +
            maker_bonus * 0.20 +
            hedge_bonus * 0.10 +
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
        """
        Valuta qualita' dati in base a quantita'.
        v10.2: soglie mercati abbassate — Data API limit=200 comprime
        la diversita' per whale che fanno molti trade/mercato (informed traders).
        """
        if n_trades >= 100 and n_markets >= 5:
            return "HIGH"
        if n_trades >= 50 and n_markets >= 3:
            return "MEDIUM"
        if n_trades >= 15 and n_markets >= 1:
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
                # v10.4: metriche avanzate
                "execution_quality": round(m.execution_quality, 4),
                "maker_ratio": round(m.maker_ratio, 4),
                "complete_set_ratio": round(m.complete_set_ratio, 4),
                "complete_set_avg_edge": round(m.complete_set_avg_edge, 4),
                "complete_set_avg_pairing_s": round(m.complete_set_avg_pairing_s, 1),
                "trader_type": m.trader_type,
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
    print("\n" + "=" * 120)
    print("  WHALE PROFILER v10.4 — Report Analisi Comportamentale")
    print("=" * 120)
    print(
        f"  {'Nome':<14} {'Score':>5} {'Rec':<5} {'TimP':>5} "
        f"{'Pattern':<13} {'Hedge':>5} {'Maker':>5} {'CS%':>4} "
        f"{'Type':<10} {'Bot':>3} {'Quality':<7} {'Trades':>6}"
    )
    print("-" * 120)

    for addr, m in sorted(wl.wallets.items(), key=lambda x: x[1].composite_score, reverse=True):
        bot_str = "Y" if m.is_likely_bot else "N"
        print(
            f"  {m.name:<14} {m.composite_score:>5.2f} {m.recommendation:<5} "
            f"{m.time_profitable_pct:>4.0%} "
            f"{m.accumulation_pattern:<13} {m.hedge_ratio:>4.0%} "
            f"{m.maker_ratio:>4.0%} {m.complete_set_ratio:>3.0%} "
            f"{m.trader_type:<10} {bot_str:>3} {m.data_quality:<7} "
            f"{m.total_trades_analyzed:>6}"
        )

    print("-" * 120)
    s = wl.summary
    print(
        f"  Totale: {s.get('total', 0)} wallet | "
        f"COPY: {s.get('copy', 0)} | "
        f"WATCH: {s.get('watch', 0)} | "
        f"SKIP: {s.get('skip', 0)}"
    )
    print("=" * 120 + "\n")


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
