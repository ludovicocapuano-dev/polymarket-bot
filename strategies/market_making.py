"""
Strategia Market Making v2.0 — Spread Capture su Polymarket
=============================================================
Piazza ordini limit BUY su YES e BUY su NO (equivalente a vendere YES),
guadagnando dallo spread tra bid e ask.

Con Builder Program: gasless + volume rewards.

Logica v2.0:
1. Trova mercati con spread 3-12% e volume 24h >= $5K
2. Preferisci mercati long-dated (>7 giorni) con bassa volatilita'
3. Piazza limit BUY YES a (best_bid + 1c) e limit BUY NO a (1 - best_ask + 1c)
4. Se entrambi fillano: profitto = spread - 2*tick
5. Gestione inventory: se sbilanciato, quoto solo il lato opposto
6. Cancel stale orders dopo 60s se non fillati

Safety:
- Non operare su mercati con eventi imminenti (<1 giorno)
- Max 5 mercati contemporanei
- Max $200 inventory per lato per mercato (scalato da auto-compound)
- Order size fisso $25 (scalato da auto-compound)
- Stale order cleanup: cancella unfilled dopo 60s
"""

import logging
import time
from dataclasses import dataclass, field

from utils.polymarket_api import Market, PolymarketAPI
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "market_making"

# ── Parametri strategia (scalati da auto-compound) ──────────────
MIN_SPREAD = 0.03             # spread minimo per operare (3%)
MAX_SPREAD = 0.12             # non quotare su mercati troppo wide (12%)
MIN_VOLUME_24H = 5000         # volume minimo 24h ($5K)
MIN_LIQUIDITY = 1000          # liquidita' minima
MAX_CONCURRENT_MARKETS = 5    # max mercati contemporanei
MAX_INVENTORY_PER_SIDE = 200  # max $ per lato per mercato (auto-compound scala)
ORDER_SIZE = 25               # size singolo ordine in $ (auto-compound scala)
MIN_DAYS_TO_EXPIRY = 1        # no mercati con eventi < 1 giorno
ORDER_TTL = 60                # cancella ordini non fillati dopo 60s
COOLDOWN_AFTER_FILL = 30      # secondi di cooldown dopo un fill
TICK = 0.01                   # tick size Polymarket


@dataclass
class MarketMakingOpportunity:
    """Un'opportunita' di market making identificata."""
    market: Market
    yes_token: str
    no_token: str
    best_bid: float          # migliore bid su YES
    best_ask: float          # migliore ask su YES
    spread: float
    mid_price: float
    days_to_expiry: float
    volume_24h: float
    book_depth_bid: float    # $ depth top 5 bids
    book_depth_ask: float    # $ depth top 5 asks

    @property
    def edge(self) -> float:
        return self.spread / 2


@dataclass
class ActiveQuote:
    """Traccia un ordine attivo (bid o ask) nel book."""
    market_id: str
    token_id: str
    side: str               # "BUY_YES" o "BUY_NO"
    order_id: str
    price: float
    size: float
    placed_at: float        # timestamp
    filled: bool = False


class MarketMakingStrategy:
    """
    Market Making v2.0: two-sided quoting via BUY YES + BUY NO.

    Su Polymarket, vendere YES equivale a comprare NO.
    Piazzando limit BUY su entrambi i token (YES e NO) catturiamo lo spread
    quando entrambi vengono fillati (YES_price + NO_price < $1.00).
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
    ):
        self.api = api
        self.risk = risk
        self._trades_executed = 0
        self._pnl_total = 0.0
        # Traccia inventory per lato: market_id -> {"yes": $, "no": $}
        self._inventory: dict[str, dict[str, float]] = {}
        # Ordini attivi: order_id -> ActiveQuote
        self._active_orders: dict[str, ActiveQuote] = {}
        # Cooldown per mercato: market_id -> timestamp
        self._market_cooldown: dict[str, float] = {}
        # PnL per mercato: market_id -> float
        self._market_pnl: dict[str, float] = {}

    def scan(self, shared_markets: list[Market] | None = None) -> list[MarketMakingOpportunity]:
        """
        Scansiona mercati per opportunita' di market making.
        Sincrono — viene chiamato dal bot nel main loop.
        """
        markets = shared_markets or []
        if not markets:
            return []

        now = time.time()

        # Cleanup cooldown scaduti
        expired = [k for k, t in self._market_cooldown.items() if now - t > COOLDOWN_AFTER_FILL]
        for k in expired:
            del self._market_cooldown[k]

        opps: list[MarketMakingOpportunity] = []

        for m in markets:
            # Cooldown attivo
            if m.id in self._market_cooldown:
                continue

            # Filtro volume 24h
            if m.volume < MIN_VOLUME_24H:
                continue

            # Liquidita'
            if m.liquidity < MIN_LIQUIDITY:
                continue

            # Token YES e NO
            yes_token = m.tokens.get("yes", "")
            no_token = m.tokens.get("no", "")
            if not yes_token or not no_token:
                continue

            # Days to expiry
            end_date = getattr(m, "end_date_iso", None) or getattr(m, "end_date", None)
            days_to_expiry = self._estimate_days_to_expiry(end_date)
            if days_to_expiry is not None and days_to_expiry < MIN_DAYS_TO_EXPIRY:
                continue

            # Inventory check — skip se gia' al limite
            inv = self._inventory.get(m.id, {"yes": 0.0, "no": 0.0})
            if inv["yes"] >= MAX_INVENTORY_PER_SIDE and inv["no"] >= MAX_INVENTORY_PER_SIDE:
                continue

            # Fetch order book per YES
            book = self.api.get_order_book(yes_token)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                continue

            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 1))
            if best_bid <= 0 or best_ask <= best_bid:
                continue

            spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2

            # Filtro spread
            if spread < MIN_SPREAD or spread > MAX_SPREAD:
                continue

            # Book depth (top 5)
            bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids[:5])
            ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:5])

            # Minimo depth per evitare mercati illiquidi
            if bid_depth < 50 or ask_depth < 50:
                continue

            opps.append(MarketMakingOpportunity(
                market=m,
                yes_token=yes_token,
                no_token=no_token,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                mid_price=mid_price,
                days_to_expiry=days_to_expiry if days_to_expiry is not None else 999.0,
                volume_24h=m.volume,
                book_depth_bid=bid_depth,
                book_depth_ask=ask_depth,
            ))

        # Ordina per spread (piu' largo = piu' profitto) ponderato per volume
        opps.sort(key=lambda o: o.spread * min(o.volume_24h, 100000), reverse=True)

        # Limita
        opps = opps[:MAX_CONCURRENT_MARKETS * 2]

        if opps:
            logger.info(
                f"[MM] Scan {len(markets)} → {len(opps)} opp MM. "
                f"Best: spread={opps[0].spread:.2%} vol=${opps[0].volume_24h:,.0f} "
                f"'{opps[0].market.question[:40]}'"
            )

        return opps

    def execute(self, opp: MarketMakingOpportunity, api: PolymarketAPI,
                risk: RiskManager, live: bool = False) -> bool:
        """
        Esegui market making: piazza limit BUY YES e limit BUY NO.

        YES side: BUY YES @ best_bid + 1c (migliora il bid di 1 tick)
        NO side:  BUY NO @ (1 - best_ask) + 1c (equivale a vendere YES a best_ask - 1c)

        Se entrambi vengono fillati: profit = spread - 2*TICK per share.
        """
        market_id = opp.market.id

        # Limita mercati contemporanei
        active_mkts = set()
        for q in self._active_orders.values():
            active_mkts.add(q.market_id)
        if len(active_mkts) >= MAX_CONCURRENT_MARKETS and market_id not in active_mkts:
            return False

        # Inventory check
        inv = self._inventory.get(market_id, {"yes": 0.0, "no": 0.0})

        # Calcola size
        size_dollars = min(ORDER_SIZE, MAX_INVENTORY_PER_SIDE - max(inv["yes"], inv["no"]))
        if size_dollars < 5:
            return False

        # Risk check
        allowed, reason = risk.can_trade(STRATEGY_NAME, size_dollars)
        if not allowed:
            logger.debug(f"[MM] Bloccato: {reason}")
            return False

        # Prezzi ordini
        # BUY YES: migliora il best bid di 1 tick
        buy_yes_price = round(opp.best_bid + TICK, 2)
        # BUY NO: equivale a SELL YES a (1 - no_price)
        # Se best_ask = 0.55, vogliamo vendere YES a 0.54 (1 tick sotto ask)
        # Quindi BUY NO a 1 - 0.54 = 0.46 → round(1 - (best_ask - TICK), 2)
        buy_no_price = round(1.0 - (opp.best_ask - TICK), 2)

        # Sanity: entrambi i prezzi devono essere 0.01-0.99
        buy_yes_price = max(0.01, min(0.99, buy_yes_price))
        buy_no_price = max(0.01, min(0.99, buy_no_price))

        # Profitto atteso per share = 1.0 - buy_yes - buy_no
        profit_per_dollar = 1.0 - buy_yes_price - buy_no_price
        if profit_per_dollar < 0.01:
            # Spread troppo stretto, non profittevole
            return False

        # Shares da comprare
        yes_shares = size_dollars / buy_yes_price
        no_shares = size_dollars / buy_no_price

        if not live:
            # Paper mode: simula
            import random
            fill_prob = 0.35
            if random.random() < fill_prob:
                pnl = size_dollars * profit_per_dollar
                self._pnl_total += pnl
                self._trades_executed += 1
                logger.info(
                    f"[MM-PAPER] FILLED spread=${profit_per_dollar:.3f} "
                    f"pnl=${pnl:.2f} total=${self._pnl_total:.2f} "
                    f"'{opp.market.question[:35]}'"
                )
                # Registra nel risk manager
                trade = Trade(
                    timestamp=time.time(), strategy=STRATEGY_NAME,
                    market_id=market_id, token_id=opp.yes_token,
                    side="BUY_YES", size=size_dollars,
                    price=buy_yes_price, edge=profit_per_dollar,
                    reason=f"MM spread={opp.spread:.3f} '{opp.market.question[:30]}'",
                )
                risk.open_trade(trade)
                risk.close_trade(opp.yes_token, won=True, pnl=pnl)
            return True

        # ── LIVE MODE: piazza ordini limit su entrambi i lati ──

        # Solo quote il lato dove non siamo al limite
        place_yes = inv["yes"] < MAX_INVENTORY_PER_SIDE
        place_no = inv["no"] < MAX_INVENTORY_PER_SIDE

        placed = 0

        if place_yes:
            try:
                result = api.buy_limit(opp.yes_token, buy_yes_price, yes_shares)
                if result:
                    order_id = ""
                    if isinstance(result, dict):
                        order_id = result.get("orderID", "") or result.get("id", "")
                    if order_id:
                        self._active_orders[order_id] = ActiveQuote(
                            market_id=market_id, token_id=opp.yes_token,
                            side="BUY_YES", order_id=order_id,
                            price=buy_yes_price, size=size_dollars,
                            placed_at=time.time(),
                        )
                    placed += 1
                    logger.info(
                        f"[MM] BUY YES {yes_shares:.1f}@${buy_yes_price:.2f} "
                        f"(${size_dollars:.0f}) '{opp.market.question[:35]}'"
                    )
            except Exception as e:
                logger.warning(f"[MM] Errore BUY YES: {e}")

        if place_no:
            try:
                result = api.buy_limit(opp.no_token, buy_no_price, no_shares)
                if result:
                    order_id = ""
                    if isinstance(result, dict):
                        order_id = result.get("orderID", "") or result.get("id", "")
                    if order_id:
                        self._active_orders[order_id] = ActiveQuote(
                            market_id=market_id, token_id=opp.no_token,
                            side="BUY_NO", order_id=order_id,
                            price=buy_no_price, size=size_dollars,
                            placed_at=time.time(),
                        )
                    placed += 1
                    logger.info(
                        f"[MM] BUY NO {no_shares:.1f}@${buy_no_price:.2f} "
                        f"(${size_dollars:.0f}) '{opp.market.question[:35]}'"
                    )
            except Exception as e:
                logger.warning(f"[MM] Errore BUY NO: {e}")

        if placed > 0:
            # Registra trade nel risk manager
            trade = Trade(
                timestamp=time.time(), strategy=STRATEGY_NAME,
                market_id=market_id, token_id=opp.yes_token,
                side="BUY_YES", size=size_dollars * placed,
                price=buy_yes_price, edge=profit_per_dollar,
                reason=f"MM spread={opp.spread:.3f} profit/share=${profit_per_dollar:.3f} "
                       f"'{opp.market.question[:30]}'",
            )
            risk.open_trade(trade)
            self._trades_executed += 1

            # Update inventory
            if market_id not in self._inventory:
                self._inventory[market_id] = {"yes": 0.0, "no": 0.0}
            if place_yes:
                self._inventory[market_id]["yes"] += size_dollars
            if place_no:
                self._inventory[market_id]["no"] += size_dollars

        return placed > 0

    def cleanup_stale_orders(self, api: PolymarketAPI):
        """Cancella ordini non fillati dopo ORDER_TTL secondi."""
        now = time.time()
        to_cancel = []

        for oid, quote in list(self._active_orders.items()):
            if not quote.filled and now - quote.placed_at > ORDER_TTL:
                to_cancel.append(oid)

        cancelled = 0
        for oid in to_cancel:
            try:
                if api.cancel_order(oid):
                    cancelled += 1
                    quote = self._active_orders.pop(oid, None)
                    if quote:
                        # Rimuovi dall'inventory se non fillato
                        inv = self._inventory.get(quote.market_id, {"yes": 0.0, "no": 0.0})
                        side_key = "yes" if quote.side == "BUY_YES" else "no"
                        inv[side_key] = max(0, inv[side_key] - quote.size)
            except Exception as e:
                logger.debug(f"[MM] Cancel error {oid[:16]}: {e}")

        if cancelled:
            logger.info(f"[MM] Cancellati {cancelled} ordini stale (>{ORDER_TTL}s)")

    def check_fills(self, api: PolymarketAPI, risk: RiskManager):
        """
        Verifica se ordini attivi sono stati fillati.
        Se entrambi i lati di un mercato sono fillati → profit.
        """
        if not self._active_orders:
            return

        try:
            open_orders = api.get_open_orders()
            open_ids = {
                o.get("orderID", "") or o.get("id", "")
                for o in open_orders
            }
        except Exception:
            return

        filled_markets: dict[str, list[ActiveQuote]] = {}

        for oid, quote in list(self._active_orders.items()):
            if quote.filled:
                continue
            # Se non e' piu' negli ordini aperti → fillato (o cancellato)
            if oid not in open_ids:
                quote.filled = True
                filled_markets.setdefault(quote.market_id, []).append(quote)
                logger.info(
                    f"[MM] FILL: {quote.side} ${quote.size:.0f}@{quote.price:.2f} "
                    f"mkt={quote.market_id[:16]}"
                )

        # Check per profitto: se ENTRAMBI i lati di un mercato sono fillati
        for mkt_id, fills in filled_markets.items():
            yes_fills = [f for f in fills if f.side == "BUY_YES"]
            no_fills = [f for f in fills if f.side == "BUY_NO"]

            if yes_fills and no_fills:
                # Entrambi i lati fillati → spread profit
                yes_cost = sum(f.size for f in yes_fills)
                no_cost = sum(f.size for f in no_fills)
                total_invested = yes_cost + no_cost
                # Per ogni $1 di shares (YES + NO che risolvono a $1), il profitto e':
                # shares * $1 - total_cost
                min_shares_value = min(yes_cost / yes_fills[0].price,
                                       no_cost / no_fills[0].price)
                pnl = min_shares_value * (1.0 - yes_fills[0].price - no_fills[0].price)
                pnl = max(0, pnl)  # Non dovrebbe essere negativo per spread capture

                self._pnl_total += pnl
                self._market_pnl[mkt_id] = self._market_pnl.get(mkt_id, 0) + pnl

                # Chiudi trade nel risk manager
                for f in yes_fills + no_fills:
                    risk.close_trade(f.token_id, won=pnl > 0, pnl=pnl / len(yes_fills + no_fills))

                # Cooldown
                self._market_cooldown[mkt_id] = time.time()

                # Reset inventory
                self._inventory[mkt_id] = {"yes": 0.0, "no": 0.0}

                logger.info(
                    f"[MM] PROFIT! ${pnl:.2f} su {mkt_id[:16]} "
                    f"(invested=${total_invested:.0f}, total_pnl=${self._pnl_total:.2f})"
                )

                # Cleanup filled orders
                for f in yes_fills + no_fills:
                    self._active_orders.pop(f.order_id, None)

    def _estimate_days_to_expiry(self, end_date) -> float | None:
        """Stima i giorni alla scadenza del mercato."""
        if not end_date:
            return None
        try:
            from datetime import datetime, timezone
            if isinstance(end_date, str):
                for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(end_date, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                else:
                    return None
            elif isinstance(end_date, (int, float)):
                dt = datetime.fromtimestamp(end_date, tz=timezone.utc)
            else:
                return None
            now = datetime.now(timezone.utc)
            return (dt - now).total_seconds() / 86400.0
        except Exception:
            return None

    @property
    def stats(self) -> dict:
        return {
            "trades": self._trades_executed,
            "pnl": self._pnl_total,
            "active_orders": len(self._active_orders),
            "markets": len(self._inventory),
            "inventory": {k: v for k, v in self._inventory.items() if v["yes"] > 0 or v["no"] > 0},
        }
