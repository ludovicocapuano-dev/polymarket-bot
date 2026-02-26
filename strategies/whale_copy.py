"""
Strategia 4: Whale Copy Trading — Replica posizioni dei top trader
==================================================================
v1.0: Monitora wallet di top trader Polymarket e copia le loro posizioni
con sizing ridotto e filtri di qualita'.

Concetto:
- I top trader Polymarket hanno win rate documentati del 55-75%
- Copiare le loro posizioni con size ridotto (10%) e ritardo minimo
  permette di catturare il loro edge informativo
- Filtri: solo wallet con win_rate >= 55% e >= 50 trade storici
- Delay massimo: 5 minuti dal trade originale del whale

Fonti:
- PolyTrack / Polywhaler: tracking wallet top trader
- Gamma API: posizioni aperte e storico trade
- Top 668 wallet Polymarket: analisi profittabilita'
"""

import logging
import random
import time
from dataclasses import dataclass

from utils.polymarket_api import Market, PolymarketAPI
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "whale_copy"

# ── Parametri strategia ──
MIN_WHALE_WIN_RATE = 0.60   # v8.0: era 0.55 — Becker: retail (<$10) ha 43.7% WR
MIN_WHALE_TRADES = 50       # trade minimi per statistiche significative
COPY_SIZE_FRACTION = 0.10   # copia al 10% del size del whale
MAX_COPY_DELAY = 120        # v7.0: max 2 minuti (era 5 — troppo ritardo erode edge)

# ── Filtro prezzo ──
MIN_TOKEN_PRICE = 0.05
MAX_TOKEN_PRICE = 0.95

# ── Whale list (indirizzi verificati da PolyTrack, CryptoSlate, DL News, GitHub) ──
TRACKED_WALLETS: dict[str, dict] = {
    # === FRENCH WHALE (Theo) — $85M+ profitto, confermato da WSJ/Bloomberg ===
    "Fredi9999": {
        "address": "0x1f2dd6d473f3e824cd2f8a89d9c69fb96f6ad0cf",
        "min_size": 1000,
        "notes": "French Whale account #1, $85M+ profit across accounts",
    },
    "Theo4": {
        "address": "0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        "min_size": 1000,
        "notes": "French Whale account #2, $12M HFT bets",
    },
    "PrincessCaro": {
        "address": "0x8119010a6e589062aa03583bb3f39ca632d9f887",
        "min_size": 1000,
        "notes": "French Whale account #3",
    },
    "Michie": {
        "address": "0xed2239a9150c3920000d0094d28fa51c7db03dd0",
        "min_size": 1000,
        "notes": "French Whale account #4",
    },
    # === TOP LEADERBOARD TRADERS ===
    "Domer": {
        "address": "0xfb351226ad501df7f3704f8e355bd000cfdf915c",
        "min_size": 500,
        "notes": "#1 all-time Polymarket volume ($300M+)",
    },
    "JustKen": {
        "address": "0xdf2b2ce2c5bf39749b01bc2f1f50a050ba2c3280",
        "min_size": 500,
        "notes": "#1 all-time, 5000+ markets traded",
    },
    "WindWalk3": {
        "address": "0x2728d99b2405a52db60160837e130b3ba3c1a83c",
        "min_size": 500,
        "notes": "$1.1M+ profit, political markets specialist",
    },
    "HyperLiquid0xb": {
        "address": "0x461f3e886dca22e561eee224d283e08b8fb47a07",
        "min_size": 500,
        "notes": "$1.4M+ profit, sports & politics",
    },
    # === NOTABLE TRADERS ===
    "Bagman": {
        "address": "0xb9d1f7a0ce19809870957a11d107e5b62017bcc3",
        "min_size": 500,
        "notes": "Top leaderboard, consistent performer",
    },
    "zxgngl": {
        "address": "0xd235973291b2b75ff4070e9c0b01728c520b0f29",
        "min_size": 500,
        "notes": "French Whale account #5, active trader",
    },
}


@dataclass
class WhaleTrade:
    """Un trade rilevato da un wallet whale."""
    whale_name: str
    wallet_address: str
    market: Market
    side: str           # "YES" o "NO"
    whale_size: float   # size in $ del trade originale
    timestamp: float    # quando il whale ha tradato
    win_rate: float     # win rate storico del whale
    total_trades: int   # numero di trade storici


@dataclass
class WhaleCopyOpportunity:
    """Opportunita' di copy trade identificata."""
    market: Market
    whale_trade: WhaleTrade
    side: str
    copy_size: float
    confidence: float
    edge: float
    reasoning: str


class WhaleCopyStrategy:
    """
    Copy trading dei top trader Polymarket.

    Monitora una lista di wallet noti (top trader) e replica le loro
    posizioni con sizing ridotto. Filtra per win_rate, numero di trade
    storici e ritardo massimo dal trade originale.

    Priorita' segnali:
    1. Whale con win_rate >= 65% e size grande → confidence alta
    2. Whale con win_rate >= 55% e track record lungo → confidence media
    3. Multipli whale sulla stessa posizione → confidence boost
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        min_edge: float = 0.03,
    ):
        self.api = api
        self.risk = risk
        self.min_edge = min_edge
        self._trades_executed = 0
        self._recently_traded: dict[str, float] = {}
        self._TRADE_COOLDOWN = 600  # 10 minuti cooldown per mercato
        # Cache degli ultimi trade dei whale rilevati
        self._last_whale_trades: list[WhaleTrade] = []
        self._last_scan_time: float = 0.0
        # Statistiche whale (cache)
        self._whale_stats: dict[str, dict] = {}
        # v8.2: Whale Profiler whitelist (additive filter)
        self._whitelist: dict[str, dict] = {}
        self._whitelist_loaded_at: float = 0.0
        self._load_whitelist()

    def _load_whitelist(self) -> None:
        """
        v8.2: Carica whitelist dal Whale Profiler (logs/whale_whitelist.json).
        Refresh ogni 6 ore. Se il file non esiste, noop (filtro additive).
        """
        try:
            from utils.whale_profiler import WhaleProfiler
            data = WhaleProfiler.load_whitelist()
            if data:
                self._whitelist = {}
                for addr, info in data.items():
                    self._whitelist[addr] = {
                        "score": info.get("composite_score", 0),
                        "recommendation": info.get("recommendation", "WATCH"),
                        "time_profitable_pct": info.get("time_profitable_pct", 0),
                        "accumulation_pattern": info.get("accumulation_pattern", "UNKNOWN"),
                    }
                self._whitelist_loaded_at = time.time()
                logger.info(
                    f"[WHALE] Whitelist caricata: {len(self._whitelist)} wallet "
                    f"(COPY={sum(1 for v in self._whitelist.values() if v['recommendation'] == 'COPY')}, "
                    f"WATCH={sum(1 for v in self._whitelist.values() if v['recommendation'] == 'WATCH')}, "
                    f"SKIP={sum(1 for v in self._whitelist.values() if v['recommendation'] == 'SKIP')})"
                )
            else:
                logger.debug("[WHALE] Nessuna whitelist trovata — filtro profiler disattivato")
        except Exception as e:
            logger.debug(f"[WHALE] Errore caricamento whitelist: {e}")

    async def scan(self, shared_markets: list[Market] | None = None) -> list[WhaleCopyOpportunity]:
        """
        Scansiona per nuovi trade dei whale monitorati.

        1. Monitora wallet noti via Gamma API o on-chain
        2. Rileva nuove posizioni aperte
        3. Filtra per win_rate >= MIN_WHALE_WIN_RATE e trades >= MIN_WHALE_TRADES
        4. Filtra per trade aperti da < MAX_COPY_DELAY secondi
        5. Calcola confidence basata su win_rate, size e storico recente
        """
        opportunities: list[WhaleCopyOpportunity] = []
        markets = shared_markets or self.api.fetch_markets(limit=200)

        if not markets:
            logger.info("[WHALE] Scan: 0 mercati disponibili")
            return []

        now = time.time()

        # Pulisci cooldown scaduti
        stale = [k for k, t in self._recently_traded.items() if now - t > 3600]
        for k in stale:
            del self._recently_traded[k]

        # Rileva nuovi trade dei whale
        whale_trades = self._detect_whale_trades(markets)

        if not whale_trades:
            logger.info(
                f"[WHALE] Scan {len(markets)} mercati, "
                f"{len(TRACKED_WALLETS)} wallet monitorati → 0 trade rilevati"
            )
            return []

        # Filtra e valuta ogni trade
        for wt in whale_trades:
            opp = self._evaluate_whale_trade(wt, now)
            if opp:
                opportunities.append(opp)

        # Boost per consensus: se multipli whale tradano lo stesso mercato/side
        opportunities = self._apply_consensus_boost(opportunities)

        # Ordina per edge * confidence
        opportunities.sort(key=lambda o: o.edge * o.confidence, reverse=True)

        if opportunities:
            logger.info(
                f"[WHALE] Scan {len(markets)} mercati → "
                f"{len(whale_trades)} trade whale rilevati → "
                f"{len(opportunities)} opportunita' copy | "
                f"migliore: edge={opportunities[0].edge:.4f} "
                f"whale={opportunities[0].whale_trade.whale_name}"
            )
        else:
            logger.info(
                f"[WHALE] Scan {len(markets)} mercati → "
                f"{len(whale_trades)} trade whale (nessuno copiabile)"
            )

        return opportunities

    def _detect_whale_trades(self, markets: list[Market]) -> list[WhaleTrade]:
        """
        Rileva nuovi trade dai wallet monitorati.

        Usa Gamma API per controllare posizioni aperte dei wallet.
        In produzione, questo andrebbe integrato con un listener on-chain
        per rilevamento in tempo reale.
        """
        trades: list[WhaleTrade] = []
        now = time.time()

        # Costruisci indice mercati per ID per lookup veloce
        market_by_id: dict[str, Market] = {m.id: m for m in markets}

        for whale_name, whale_info in TRACKED_WALLETS.items():
            address = whale_info.get("address", "")
            if not address:
                continue

            min_size = whale_info.get("min_size", 500)

            # Fetch posizioni recenti del wallet via API
            recent_positions = self._fetch_wallet_positions(address)
            if not recent_positions:
                continue

            # Fetch statistiche del wallet (con cache)
            stats = self._get_whale_stats(address)
            win_rate = stats.get("win_rate", 0.0)
            total_trades = stats.get("total_trades", 0)

            for pos in recent_positions:
                market_id = pos.get("market_id", "")
                side = pos.get("side", "")
                size = pos.get("size", 0.0)
                trade_time = pos.get("timestamp", 0.0)

                # Filtri base
                if size < min_size:
                    continue
                if market_id not in market_by_id:
                    continue
                if side not in ("YES", "NO"):
                    continue

                trades.append(WhaleTrade(
                    whale_name=whale_name,
                    wallet_address=address,
                    market=market_by_id[market_id],
                    side=side,
                    whale_size=size,
                    timestamp=trade_time,
                    win_rate=win_rate,
                    total_trades=total_trades,
                ))

        self._last_whale_trades = trades
        self._last_scan_time = now
        return trades

    def _fetch_wallet_positions(self, address: str) -> list[dict]:
        """
        Fetch posizioni recenti di un wallet via Polymarket data API.

        v9.2.1: Migrato da gamma-api (404) a data-api.polymarket.com.
        Endpoint: GET /activity?user=ADDRESS&limit=N
        Response: [{proxyWallet, timestamp, conditionId, type, size, usdcSize,
                    price, side, outcomeIndex, title, slug, ...}]

        Returns lista di dict con: market_id, side, size, timestamp.
        """
        import requests as _req
        positions = []
        try:
            # v9.2.1: data-api.polymarket.com (gamma-api /activity è 404)
            resp = _req.get(
                "https://data-api.polymarket.com/activity",
                params={"user": address, "limit": 20},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.warning(f"[WHALE] data-api HTTP {resp.status_code} per {address[:10]}...")
                return []

            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", data.get("positions", []))

            now = time.time()
            for item in items:
                # v9.2.1: Filtra solo BUY (non REDEEM, SELL, etc.)
                trade_type = item.get("type", "").upper()
                if trade_type not in ("BUY", "TRADE", ""):
                    continue

                # Parse conditionId (data-api usa conditionId)
                market_id = (
                    item.get("conditionId", "") or
                    item.get("market_id", "") or
                    item.get("marketId", "") or
                    item.get("condition_id", "")
                )
                if not market_id:
                    continue

                # Side: data-api usa outcomeIndex (0=YES, 1=NO) e side ("BUY"/"SELL")
                side = item.get("side", item.get("outcome", "")).upper()
                if side in ("BUY", "LONG", ""):
                    # Determina YES/NO da outcomeIndex
                    idx = item.get("outcomeIndex", item.get("outcome_index", -1))
                    if idx == 0:
                        side = "YES"
                    elif idx == 1:
                        side = "NO"
                    elif side in ("YES", "NO"):
                        pass  # già corretto
                    else:
                        continue
                elif side == "SELL":
                    continue  # non copiamo le vendite
                elif side not in ("YES", "NO"):
                    continue

                # Size in $ (data-api usa usdcSize)
                size = float(item.get("usdcSize", item.get("size", item.get("value", 0))))
                if size <= 0:
                    continue

                # Timestamp (data-api usa unix timestamp in secondi)
                ts_raw = item.get("timestamp", item.get("createdAt", item.get("created_at", "")))
                if isinstance(ts_raw, (int, float)):
                    ts = float(ts_raw)
                    if ts > 1e12:  # millisecondi
                        ts /= 1000
                elif isinstance(ts_raw, str) and ts_raw:
                    try:
                        from datetime import datetime, timezone
                        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    except Exception:
                        ts = now
                else:
                    ts = now

                # Solo trade recenti (ultimi MAX_COPY_DELAY secondi)
                if now - ts > MAX_COPY_DELAY:
                    continue

                positions.append({
                    "market_id": market_id,
                    "side": side,
                    "size": size,
                    "timestamp": ts,
                })

        except Exception as e:
            logger.warning(f"[WHALE] Errore fetch posizioni {address[:10]}...: {e}")

        return positions

    def _get_whale_stats(self, address: str) -> dict:
        """
        Ottieni statistiche del wallet da Gamma API o profilo Polymarket.
        Cache per 1 ora per evitare chiamate ripetute.
        """
        import requests as _req

        if address in self._whale_stats:
            cached = self._whale_stats[address]
            if time.time() - cached.get("fetched_at", 0) < 3600:
                return cached

        stats = {
            "win_rate": 0.0,
            "total_trades": 0,
            "avg_size": 0.0,
            "pnl_30d": 0.0,
            "fetched_at": time.time(),
        }

        try:
            # v9.2.1: data-api.polymarket.com (gamma-api /profiles è 404)
            resp = _req.get(
                f"https://data-api.polymarket.com/profiles/{address}",
                timeout=8,
            )
            if resp.status_code == 200:
                profile = resp.json()
                # Prova diversi formati di risposta
                stats["total_trades"] = int(profile.get(
                    "totalTrades", profile.get("total_trades",
                    profile.get("marketsTraded", profile.get("numTrades", 0)))
                ))
                stats["pnl_30d"] = float(profile.get(
                    "pnl", profile.get("profit", profile.get("totalPnl", 0))
                ))

                # Win rate: se non direttamente disponibile, stima
                wins = float(profile.get("wins", profile.get("marketsWon", 0)))
                losses = float(profile.get("losses", profile.get("marketsLost", 0)))
                if wins + losses > 0:
                    stats["win_rate"] = wins / (wins + losses)
                elif stats["total_trades"] > 0 and stats["pnl_30d"] > 0:
                    # v10.2: PnL positivo su wallet curato → fallback sopra soglia
                    stats["win_rate"] = 0.62
                else:
                    # v10.2: nessun dato verificabile → rifiuta (0.0 < MIN_WHALE_WIN_RATE)
                    stats["win_rate"] = 0.0
                    logger.warning(
                        f"[WHALE] No win/loss data per {address[:10]}... — win_rate=0.0 (reject)"
                    )

        except Exception as e:
            logger.warning(f"[WHALE] Errore fetch stats {address[:10]}...: {e}")
            # v10.2: API failure → nessun dato verificabile, rifiuta
            stats["win_rate"] = 0.0
            stats["total_trades"] = 0

        self._whale_stats[address] = stats
        return stats

    def _evaluate_whale_trade(
        self, wt: WhaleTrade, now: float
    ) -> WhaleCopyOpportunity | None:
        """
        Valuta se un trade whale e' copiabile.

        Filtri:
        - win_rate >= MIN_WHALE_WIN_RATE
        - total_trades >= MIN_WHALE_TRADES
        - trade entro MAX_COPY_DELAY secondi
        - prezzo nel range accettabile
        - mercato non in cooldown
        """
        # Filtro win rate e track record
        if wt.win_rate < MIN_WHALE_WIN_RATE:
            return None
        if wt.total_trades < MIN_WHALE_TRADES:
            return None

        # Filtro delay
        delay = now - wt.timestamp
        if delay > MAX_COPY_DELAY:
            return None

        # Filtro cooldown mercato
        if wt.market.id in self._recently_traded:
            if now - self._recently_traded[wt.market.id] < self._TRADE_COOLDOWN:
                return None

        # v8.2: Filtro whitelist profiler (additive — se whitelist non esiste, skip)
        if self._whitelist:
            # Refresh whitelist ogni 6 ore
            if now - self._whitelist_loaded_at > 21600:
                self._load_whitelist()

            wl_entry = self._whitelist.get(wt.wallet_address)
            if wl_entry and wl_entry.get("recommendation") == "SKIP" and wl_entry.get("score", 0) > 0:
                # v10.2: ignora SKIP se score=0 (data_quality=INSUFFICIENT)
                logger.debug(
                    f"[WHALE] SKIP profiler: {wt.whale_name} "
                    f"score={wl_entry.get('score', 0):.2f}"
                )
                return None

        # Filtro prezzo
        token_key = "yes" if wt.side == "YES" else "no"
        price = wt.market.prices.get(token_key, 0.5)
        if price < MIN_TOKEN_PRICE or price > MAX_TOKEN_PRICE:
            return None

        # Non copiare se il whale ha gia' posizione aperta sullo stesso mercato
        for open_t in self.risk.open_trades:
            if open_t.market_id == wt.market.id:
                return None

        # Calcola confidence basata su win_rate e size
        confidence = self._compute_confidence(wt, delay)

        # Calcola edge stimato basato sul win_rate storico del whale
        # Edge = win_rate - (1 - win_rate) = 2 * win_rate - 1, scalato
        raw_edge = (wt.win_rate - 0.50) * 0.5  # conservativo: meta' dell'edge teorico
        # Aggiusta per delay (piu' tardi = meno edge)
        delay_factor = max(0.5, 1.0 - (delay / MAX_COPY_DELAY) * 0.5)
        edge = raw_edge * delay_factor

        if edge < self.min_edge:
            return None

        # v8.0: Copy fraction adattiva per whale size (Becker Dataset)
        if wt.whale_size >= 50_000:
            copy_frac = 0.05   # Solo 5% per whale molto grossi (meno informativi)
        elif wt.whale_size >= 10_000:
            copy_frac = 0.08   # 8% per whale nel sweet spot
        else:
            copy_frac = COPY_SIZE_FRACTION  # 10% standard

        copy_size = min(
            wt.whale_size * copy_frac,
            self.risk.config.max_bet_size,
        )

        return WhaleCopyOpportunity(
            market=wt.market,
            whale_trade=wt,
            side=wt.side,
            copy_size=copy_size,
            confidence=confidence,
            edge=edge,
            reasoning=(
                f"WHALE_COPY: {wt.whale_name} "
                f"WR={wt.win_rate:.0%} ({wt.total_trades}t) "
                f"{wt.side}@{price:.3f} "
                f"whale_size=${wt.whale_size:,.0f} "
                f"delay={delay:.0f}s "
                f"'{wt.market.question[:35]}'"
            ),
        )

    def _compute_confidence(self, wt: WhaleTrade, delay: float) -> float:
        """Calcola confidence basata su qualita' del whale e freschezza."""
        confidence = 0.50  # base

        # Boost per win_rate alto
        if wt.win_rate >= 0.70:
            confidence += 0.15
        elif wt.win_rate >= 0.65:
            confidence += 0.10
        elif wt.win_rate >= 0.60:
            confidence += 0.05

        # Boost per track record lungo
        if wt.total_trades >= 200:
            confidence += 0.08
        elif wt.total_trades >= 100:
            confidence += 0.05

        # v8.0: Size-aware confidence (Becker Dataset)
        # Sweet spot: $1K-$100K (68.4% WR), $100K+ scende a 64.2%
        if wt.whale_size > 100_000:
            # Mega-whale: rendimenti decrescenti
            confidence *= 0.70
        elif wt.whale_size < 100:
            # Trade troppo piccoli: poco informativi
            confidence *= 0.50
        elif wt.whale_size >= 5000:
            confidence += 0.08
        elif wt.whale_size >= 2000:
            confidence += 0.05

        # Penalita' per delay alto
        if delay > 180:
            confidence -= 0.05
        elif delay > 60:
            confidence -= 0.02

        # v8.2: Boost/penalita' da Whale Profiler whitelist
        if self._whitelist:
            wl_entry = self._whitelist.get(wt.wallet_address)
            if wl_entry:
                rec = wl_entry.get("recommendation", "")
                score = wl_entry.get("score", 0)
                pattern = wl_entry.get("accumulation_pattern", "")

                if rec == "COPY" and score >= 0.75:
                    confidence += 0.08
                elif rec == "COPY":
                    confidence += 0.05
                elif rec == "WATCH":
                    confidence -= 0.03

                if pattern == "INCREASING":
                    confidence += 0.03

        return min(max(confidence, 0.40), 0.85)

    def _apply_consensus_boost(
        self, opportunities: list[WhaleCopyOpportunity]
    ) -> list[WhaleCopyOpportunity]:
        """
        Boost confidence quando multipli whale tradano lo stesso mercato/side.
        Consensus tra trader indipendenti e' un segnale molto forte.
        """
        # Raggruppa per (market_id, side)
        groups: dict[tuple[str, str], list[WhaleCopyOpportunity]] = {}
        for opp in opportunities:
            key = (opp.market.id, opp.side)
            groups.setdefault(key, []).append(opp)

        boosted: list[WhaleCopyOpportunity] = []
        for key, group in groups.items():
            if len(group) >= 2:
                # Multipli whale sullo stesso trade → boost
                for opp in group:
                    opp.confidence = min(opp.confidence + 0.10, 0.90)
                    opp.edge = min(opp.edge * 1.2, 0.15)
                    opp.reasoning += f" [CONSENSUS: {len(group)} whale]"
            boosted.extend(group)

        return boosted

    async def execute(self, opp: WhaleCopyOpportunity, paper: bool = True) -> bool:
        """
        Esegui un copy trade.

        Size = min(whale_size * COPY_SIZE_FRACTION, max_bet_size)
        Paper simulation: usa il win_rate storico del whale come sim_win_prob.
        """
        now = time.time()
        market_id = opp.market.id
        last_traded = self._recently_traded.get(market_id, 0)

        if now - last_traded < self._TRADE_COOLDOWN:
            return False

        # Non copiare se c'e' gia' posizione aperta
        for open_t in self.risk.open_trades:
            if open_t.market_id == market_id:
                return False

        token_key = "yes" if opp.side == "YES" else "no"
        token_id = opp.market.tokens[token_key]
        price = opp.market.prices[token_key]

        if price < MIN_TOKEN_PRICE or price > MAX_TOKEN_PRICE:
            return False

        # Sizing: usa copy_size calcolato, ma verifica con Kelly
        win_prob = min(price + opp.edge, 0.95)
        kelly_size = self.risk.kelly_size(
            win_prob=win_prob,
            price=price,
            strategy=STRATEGY_NAME,
            is_maker=True,
        )

        # Usa il minore tra copy_size e Kelly size
        size = min(opp.copy_size, kelly_size) if kelly_size > 0 else 0
        if size == 0:
            logger.info(
                f"[WHALE] kelly_size=0 '{opp.market.question[:35]}' "
                f"p={price:.3f} wp={win_prob:.3f} e={opp.edge:.3f}"
            )
            return False

        allowed, reason = self.risk.can_trade(STRATEGY_NAME, size, price=price, side=f"BUY_{opp.side}", market_id=opp.market.id)
        if not allowed:
            logger.info(f"[WHALE] Trade bloccato: {reason}")
            return False

        trade = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id=opp.market.id,
            token_id=token_id,
            side=f"BUY_{opp.side}",
            size=size,
            price=price,
            edge=opp.edge,
            reason=f"[WHALE_COPY] {opp.reasoning}",
        )

        if paper:
            logger.info(
                f"[PAPER] WHALE_COPY: BUY {opp.side} "
                f"'{opp.market.question[:35]}' "
                f"${size:.2f} @{price:.4f} edge={opp.edge:.4f} "
                f"whale={opp.whale_trade.whale_name}"
            )
            self.risk.open_trade(trade)

            # Paper simulation: usa win_rate del whale come proxy
            wt = opp.whale_trade
            sim_win_prob = min(max(wt.win_rate * 0.9, 0.45), 0.78)
            won = random.random() < sim_win_prob
            slippage = 0.93 + random.random() * 0.05
            if won:
                raw_mult = (1.0 / price) - 1.0
                capped_mult = min(raw_mult, 20.0)
                pnl = size * capped_mult * slippage
            else:
                pnl = -size * slippage
            self.risk.close_trade(token_id, won=won, pnl=pnl)
        else:
            result = self.api.smart_buy(
                token_id, size, target_price=price,
                timeout_sec=10.0, fallback_market=True,
            )
            if result:
                # v7.4: Aggiorna prezzo con fill reale dal CLOB
                if isinstance(result, dict) and result.get("_fill_price"):
                    trade.price = result["_fill_price"]
                self.risk.open_trade(trade)

        self._recently_traded[market_id] = time.time()
        self._trades_executed += 1
        return True

    @property
    def stats(self) -> dict:
        return {
            "trades_executed": self._trades_executed,
            "tracked_wallets": len(TRACKED_WALLETS),
            "whale_trades_last_scan": len(self._last_whale_trades),
        }
