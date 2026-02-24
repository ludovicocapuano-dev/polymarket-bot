"""
Feed real-time prezzi Polymarket via WebSocket.

Architettura v9.2:
- WebSocket push per price updates (~100ms latenza vs ~3s REST)
- Approccio ibrido: REST per struttura mercati, WS per prezzi real-time
- Connessioni multiple: max 500 asset per connessione
- Graceful degradation: se WS down, bot torna a REST ogni ciclo
- Pattern: segue binance_feed.py (websockets + asyncio)

URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_ASSETS_PER_CONN = 500
REST_REVALIDATION_INTERVAL = 60  # secondi
STALE_THRESHOLD = 30  # secondi senza update = dato stale


@dataclass
class TokenState:
    """Stato real-time di un singolo token (yes o no)."""
    token_id: str
    market_id: str
    side: str  # "yes" / "no"
    price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    last_trade_price: float = 0.0
    updated_at: float = 0.0


class PolymarketWSFeed:
    """
    Feed WebSocket per prezzi Polymarket in push.

    Uso:
        ws_feed = PolymarketWSFeed()
        ws_feed.register_markets(markets)  # popola token da REST
        await ws_feed.connect()            # loop WS in asyncio.gather

    Le strategie non sanno se i prezzi vengono da REST o WS:
        markets = ws_feed.update_prices(cached_markets)
    """

    def __init__(self):
        self._tokens: dict[str, TokenState] = {}       # token_id -> TokenState
        self._market_tokens: dict[str, list[str]] = {}  # market_id -> [yes_tid, no_tid]
        self._running: bool = False
        self._connected: bool = False
        self._messages_received: int = 0
        self._last_message_at: float = 0.0
        self._ws_connections: list = []

    def register_markets(self, markets: list) -> None:
        """
        Popola _tokens e _market_tokens dai mercati REST.
        Chiamato dopo il primo fetch e ogni 20 cicli per sincronizzare.
        """
        new_count = 0
        for m in markets:
            yes_tid = m.tokens.get("yes", "")
            no_tid = m.tokens.get("no", "")
            if not yes_tid or not no_tid:
                continue

            if yes_tid not in self._tokens:
                self._tokens[yes_tid] = TokenState(
                    token_id=yes_tid, market_id=m.id, side="yes",
                    price=m.prices.get("yes", 0.0),
                )
                new_count += 1
            if no_tid not in self._tokens:
                self._tokens[no_tid] = TokenState(
                    token_id=no_tid, market_id=m.id, side="no",
                    price=m.prices.get("no", 0.0),
                )
                new_count += 1

            self._market_tokens[m.id] = [yes_tid, no_tid]

        if new_count > 0:
            logger.info(
                f"[WS-POLY] Registrati {new_count} nuovi token "
                f"(totale: {len(self._tokens)} token, "
                f"{len(self._market_tokens)} mercati)"
            )

    async def connect(self) -> None:
        """Loop principale WS. Va in asyncio.gather col bot."""
        self._running = True

        # Attendi che register_markets() popoli i token
        while self._running and not self._tokens:
            await asyncio.sleep(1.0)

        if not self._running:
            return

        logger.info(
            f"[WS-POLY] Avvio WebSocket feed per "
            f"{len(self._tokens)} token"
        )

        while self._running:
            try:
                groups = self._build_subscription_groups()
                if not groups:
                    await asyncio.sleep(5)
                    continue

                tasks = []
                for idx, group in enumerate(groups):
                    tasks.append(self._ws_connection(idx, group))

                await asyncio.gather(*tasks)

            except Exception as e:
                logger.error(f"[WS-POLY] Errore gather: {e}")
                self._connected = False
                if self._running:
                    await asyncio.sleep(5)

    async def _ws_connection(self, group_idx: int, asset_ids: list[str]) -> None:
        """Una singola connessione WS per un gruppo di asset."""
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=10
                ) as ws:
                    # Subscription message
                    sub_msg = json.dumps({
                        "assets_ids": asset_ids,
                        "type": "market",
                    })
                    await ws.send(sub_msg)

                    self._connected = True
                    logger.info(
                        f"[WS-POLY] Connessione #{group_idx} attiva "
                        f"({len(asset_ids)} asset)"
                    )

                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            self._handle_message(data)
                        except json.JSONDecodeError:
                            logger.debug(f"[WS-POLY] Messaggio non-JSON ignorato")

            except websockets.ConnectionClosed:
                logger.warning(
                    f"[WS-POLY] Connessione #{group_idx} chiusa, "
                    f"riconnessione in 2s..."
                )
                self._connected = False
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(
                    f"[WS-POLY] Errore connessione #{group_idx}: {e}"
                )
                self._connected = False
                await asyncio.sleep(5)

    def _handle_message(self, data: dict) -> None:
        """Router per i diversi tipi di evento WS."""
        self._messages_received += 1
        self._last_message_at = time.time()

        # Polymarket WS invia liste di eventi
        events = data if isinstance(data, list) else [data]

        for event in events:
            event_type = event.get("event_type", "")
            asset_id = event.get("asset_id", "")

            if not asset_id or asset_id not in self._tokens:
                continue

            token = self._tokens[asset_id]
            now = time.time()

            if event_type == "book":
                # Snapshot order book
                bids = event.get("bids", [])
                asks = event.get("asks", [])
                if bids:
                    token.best_bid = float(bids[0].get("price", 0))
                if asks:
                    token.best_ask = float(asks[0].get("price", 0))
                # Aggiorna price dal midpoint se abbiamo bid/ask
                if token.best_bid > 0 and token.best_ask > 0:
                    token.price = (token.best_bid + token.best_ask) / 2
                elif token.best_bid > 0:
                    token.price = token.best_bid
                token.updated_at = now

            elif event_type == "price_change":
                price = event.get("price")
                if price is not None:
                    token.price = float(price)
                bid = event.get("bid")
                ask = event.get("ask")
                if bid is not None:
                    token.best_bid = float(bid)
                if ask is not None:
                    token.best_ask = float(ask)
                token.updated_at = now

            elif event_type == "last_trade_price":
                ltp = event.get("last_trade_price")
                if ltp is not None:
                    token.last_trade_price = float(ltp)
                    # Aggiorna price se non abbiamo dati book freschi
                    if now - token.updated_at > 5:
                        token.price = float(ltp)
                    token.updated_at = now

            elif event_type == "tick_size_change":
                # Ignoriamo, non rilevante per i prezzi
                pass
            else:
                # Evento generico: se ha un campo price, aggiorniamo
                price = event.get("price") or event.get("last_trade_price")
                if price is not None:
                    try:
                        token.price = float(price)
                        token.updated_at = now
                    except (ValueError, TypeError):
                        pass

    def update_prices(self, markets: list) -> list:
        """
        Sovrascrive market.prices con dati WS se freschi (< STALE_THRESHOLD).
        Le strategie non sanno se i prezzi vengono da REST o WS.
        """
        now = time.time()
        updated = 0

        for market in markets:
            tids = self._market_tokens.get(market.id)
            if not tids or len(tids) < 2:
                continue

            yes_token = self._tokens.get(tids[0])
            no_token = self._tokens.get(tids[1])

            if not yes_token or not no_token:
                continue

            # Usa dati WS solo se freschi
            yes_fresh = yes_token.updated_at > 0 and (now - yes_token.updated_at) < STALE_THRESHOLD
            no_fresh = no_token.updated_at > 0 and (now - no_token.updated_at) < STALE_THRESHOLD

            if yes_fresh and yes_token.price > 0:
                market.prices["yes"] = yes_token.price
                updated += 1
            if no_fresh and no_token.price > 0:
                market.prices["no"] = no_token.price

        if updated > 0:
            logger.debug(f"[WS-POLY] Aggiornati prezzi per {updated} mercati via WS")

        return markets

    def _build_subscription_groups(self) -> list[list[str]]:
        """Divide i token in gruppi da MAX_ASSETS_PER_CONN."""
        all_ids = list(self._tokens.keys())
        groups = []
        for i in range(0, len(all_ids), MAX_ASSETS_PER_CONN):
            groups.append(all_ids[i:i + MAX_ASSETS_PER_CONN])
        return groups

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def available(self) -> bool:
        """WS e' disponibile se connesso e ultimo messaggio < STALE_THRESHOLD."""
        if not self._connected:
            return False
        if self._last_message_at == 0:
            return False
        return (time.time() - self._last_message_at) < STALE_THRESHOLD

    def stats(self) -> dict:
        """Statistiche del feed WS."""
        now = time.time()
        fresh_tokens = sum(
            1 for t in self._tokens.values()
            if t.updated_at > 0 and (now - t.updated_at) < STALE_THRESHOLD
        )
        return {
            "connected": self._connected,
            "available": self.available,
            "messages_received": self._messages_received,
            "total_tokens": len(self._tokens),
            "fresh_tokens": fresh_tokens,
            "last_message_age": round(now - self._last_message_at, 1) if self._last_message_at > 0 else -1,
            "markets_tracked": len(self._market_tokens),
        }

    async def stop(self) -> None:
        """Ferma il feed WS."""
        self._running = False
        self._connected = False
        logger.info(
            f"[WS-POLY] Feed fermato. Stats: {self._messages_received} messaggi, "
            f"{len(self._tokens)} token tracciati"
        )
