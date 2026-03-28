"""
Telegram Alert Module — Non-blocking trade/drawdown/summary notifications.

Sends formatted alerts to a Telegram chat via Bot API.
Queue-based architecture: callers push messages, a background task drains and sends.

Rate limit: 20 messages/minute (Telegram API limit is 30, we stay conservative).

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Without either: module disables silently (noop), bot runs normally.

Integration points in bot.py (DO NOT modify bot.py automatically):
  1. Import:  line ~35  -> from utils.telegram_alert import TelegramAlert
  2. Init:    line ~212 -> self.telegram = TelegramAlert()
  3. Gather:  line ~571 -> add self.telegram.connect() to asyncio.gather
  4. After each execute() call -> self.telegram.trade_alert(...)
  5. Daily PnL reporting section -> self.telegram.daily_summary(...)
"""

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# ── Rate limit config ──────────────────────────────────────────
MAX_MESSAGES_PER_MINUTE = 20
RATE_WINDOW_SECONDS = 60
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.0
SEND_TIMEOUT_SECONDS = 10


def _escape_md2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    out = []
    for ch in text:
        if ch in special:
            out.append("\\")
        out.append(ch)
    return "".join(out)


@dataclass
class TelegramAlert:
    """Async Telegram alerting with queue and rate limiting."""

    _disabled: bool = False
    _bot_token: str = ""
    _chat_id: str = ""
    _queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _rate_limiter: deque = field(default_factory=deque)  # timestamps of sent messages

    def __post_init__(self) -> None:
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not self._bot_token or not self._chat_id:
            self._disabled = True
            logger.info("TelegramAlert disabled — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        else:
            logger.info("TelegramAlert initialized (chat_id=%s)", self._chat_id)

    # ── Background loop ────────────────────────────────────────

    async def connect(self) -> None:
        """Background loop: drain queue and send messages respecting rate limit."""
        if self._disabled:
            return
        logger.info("TelegramAlert background sender started")
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS) as client:
            while True:
                try:
                    text = await self._queue.get()
                    await self._wait_for_rate_limit()
                    await self._send(client, text)
                    self._queue.task_done()
                except asyncio.CancelledError:
                    logger.info("TelegramAlert sender cancelled, draining remaining %d msgs", self._queue.qsize())
                    break
                except Exception:
                    logger.exception("TelegramAlert sender error")
                    await asyncio.sleep(1)

    async def _wait_for_rate_limit(self) -> None:
        """Block until we are under the rate limit."""
        while True:
            now = time.monotonic()
            # Purge old timestamps outside the window
            while self._rate_limiter and self._rate_limiter[0] < now - RATE_WINDOW_SECONDS:
                self._rate_limiter.popleft()
            if len(self._rate_limiter) < MAX_MESSAGES_PER_MINUTE:
                return
            # Wait until the oldest message falls outside the window
            sleep_for = self._rate_limiter[0] - (now - RATE_WINDOW_SECONDS) + 0.1
            await asyncio.sleep(sleep_for)

    # ── Send with retry ────────────────────────────────────────

    async def _send(self, client: httpx.AsyncClient, text: str) -> None:
        """HTTP POST to Telegram sendMessage with retry."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    self._rate_limiter.append(time.monotonic())
                    return
                if resp.status_code == 429:
                    # Telegram asks us to slow down
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    logger.warning("TelegramAlert rate limited by Telegram, retry after %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                logger.warning(
                    "TelegramAlert send failed (attempt %d/%d): HTTP %d — %s",
                    attempt, MAX_RETRIES, resp.status_code, resp.text[:200],
                )
            except httpx.HTTPError as exc:
                logger.warning("TelegramAlert HTTP error (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        logger.error("TelegramAlert gave up sending message after %d attempts", MAX_RETRIES)

    # ── Public alert methods ───────────────────────────────────

    def trade_alert(
        self,
        strategy: str,
        side: str,
        size: float,
        price: float,
        edge: float,
        market_name: str,
        kelly: float = 0.0,
    ) -> None:
        """Queue a trade execution alert."""
        if self._disabled:
            return
        e = _escape_md2
        msg = (
            f"\U0001f7e2 *TRADE EXECUTED*\n"
            f"Strategy: {e(strategy)}\n"
            f"Side: {e(side)} \\| ${e(f'{size:.2f}')} @ {e(f'{price:.3f}')}\n"
            f"Edge: {e(f'{edge:.1%}')} \\| Kelly: {e(f'{kelly:.2f}')}\n"
            f"Market: {e(market_name)}"
        )
        self._enqueue(msg)

    def drawdown_alert(self, current_drawdown_pct: float, daily_pnl: float) -> None:
        """Queue a drawdown warning alert."""
        if self._disabled:
            return
        e = _escape_md2
        if current_drawdown_pct >= 20:
            action = "HALTING TRADING"
        elif current_drawdown_pct >= 15:
            action = "Reducing position sizes"
        else:
            action = "Monitoring"
        msg = (
            f"\u26a0\ufe0f *DRAWDOWN ALERT*\n"
            f"Daily drawdown: {e(f'{current_drawdown_pct:+.1f}%')}\n"
            f"Daily PnL: {e(f'${daily_pnl:+,.2f}')}\n"
            f"Action: {e(action)}"
        )
        self._enqueue(msg)

    def daily_summary(
        self,
        total_pnl: float,
        trades_count: int,
        win_rate: float,
        capital: float,
    ) -> None:
        """Queue the end-of-day summary."""
        if self._disabled:
            return
        e = _escape_md2
        from datetime import datetime

        date_str = datetime.utcnow().strftime("%b %d")
        pnl_sign = "\\+" if total_pnl >= 0 else ""
        msg = (
            f"\U0001f4ca *DAILY SUMMARY \u2014 {e(date_str)}*\n"
            f"Trades: {e(str(trades_count))} \\| WR: {e(f'{win_rate:.1%}')}\n"
            f"PnL: {pnl_sign}{e(f'${abs(total_pnl):,.2f}')}\n"
            f"Capital: {e(f'${capital:,.2f}')}"
        )
        self._enqueue(msg)

    def error_alert(self, error_msg: str) -> None:
        """Queue an error notification."""
        if self._disabled:
            return
        e = _escape_md2
        # Truncate long error messages
        truncated = error_msg[:500] if len(error_msg) > 500 else error_msg
        msg = f"\U0001f534 *ERROR*\n```\n{e(truncated)}\n```"
        self._enqueue(msg)

    async def notify_startup(
        self,
        mode: str,
        capital: float,
        strategies: list[str],
    ) -> None:
        """Send a startup notification (called once at boot, awaitable)."""
        if self._disabled:
            return
        e = _escape_md2
        strat_list = ", ".join(e(s) for s in strategies)
        msg = (
            f"\U0001f680 *BOT STARTED \\({e(mode)}\\)*\n"
            f"Capital: {e(f'${capital:,.2f}')}\n"
            f"Strategies: {e(str(len(strategies)))} active\n"
            f"{strat_list}"
        )
        # For startup we send directly instead of queueing,
        # because the background loop may not be running yet.
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS) as client:
            await self._send(client, msg)
            self._rate_limiter.append(time.monotonic())

    # ── Internal ───────────────────────────────────────────────

    def _enqueue(self, text: str) -> None:
        """Put a message on the queue (non-blocking, fire-and-forget)."""
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.warning("TelegramAlert queue full, dropping message")
