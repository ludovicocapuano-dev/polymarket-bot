"""
WebSocket Price Feed → Database (v12.8)
========================================
Real-time price updates from Polymarket CLOB WebSocket,
stored directly into the SQLite database.

Handles reconnection, idempotency (INSERT OR IGNORE),
and subscription management.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Set

try:
    import websockets
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

from utils.market_db import db

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WSPriceFeed:
    """Real-time WebSocket price feed → SQLite."""

    def __init__(self, on_update: Optional[Callable] = None):
        self.on_update = on_update
        self.subscribed_tokens: Set[str] = set()
        self._ws = None
        self._running = False
        self._reconnect_delay = 1
        self._message_count = 0

    async def subscribe(self, token_ids: list[str]):
        """Add tokens to subscription."""
        new_tokens = set(token_ids) - self.subscribed_tokens
        self.subscribed_tokens.update(new_tokens)
        if self._ws and new_tokens:
            await self._send_subscription(list(new_tokens))

    async def _send_subscription(self, token_ids: list[str]):
        msg = {"assets_ids": token_ids, "type": "Market"}
        await self._ws.send(json.dumps(msg))
        logger.info(f"[WS-FEED] Subscribed to {len(token_ids)} tokens")

    async def connect(self):
        """Main connection loop with auto-reconnect."""
        if not WS_AVAILABLE:
            logger.warning("[WS-FEED] websockets not installed, skipping")
            return

        self._running = True
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=30, ping_timeout=10, close_timeout=5
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1

                    if self.subscribed_tokens:
                        await self._send_subscription(list(self.subscribed_tokens))

                    logger.info(f"[WS-FEED] Connected, listening...")
                    async for message in ws:
                        await self._handle_message(message)

            except Exception as e:
                logger.debug(f"[WS-FEED] Error: {e}")
            finally:
                self._ws = None

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
            self._message_count += 1
            events = data if isinstance(data, list) else [data]

            for event in events:
                etype = event.get("event_type") or event.get("type", "")

                if etype == "price_change":
                    self._store_price(event)
                elif etype == "book":
                    self._store_book(event)
        except Exception as e:
            logger.debug(f"[WS-FEED] Parse error: {e}")

    def _store_price(self, event: dict):
        token_id = event.get("asset_id", "")
        price = float(event.get("price", 0))
        if not token_id or price <= 0:
            return

        # Lookup market context
        with db.connection() as conn:
            row = conn.execute(
                "SELECT market_id, outcome FROM outcome_tokens WHERE token_id = ?",
                (token_id,)
            ).fetchone()

        if row:
            db.record_price(token_id, row["market_id"], row["outcome"],
                            price, source="ws")

    def _store_book(self, event: dict):
        token_id = event.get("asset_id", "")
        bids = event.get("bids", [])
        asks = event.get("asks", [])
        if not bids or not asks:
            return

        try:
            best_bid = max(float(b["price"]) for b in bids)
            best_ask = min(float(a["price"]) for a in asks)
            mid = (best_bid + best_ask) / 2

            with db.connection() as conn:
                row = conn.execute(
                    "SELECT market_id, outcome FROM outcome_tokens WHERE token_id = ?",
                    (token_id,)
                ).fetchone()

            if row:
                db.record_price(token_id, row["market_id"], row["outcome"],
                                mid, best_bid, best_ask, source="ws")
        except Exception:
            pass

    def stop(self):
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "connected": self._ws is not None,
            "subscribed_tokens": len(self.subscribed_tokens),
            "messages_received": self._message_count,
        }
