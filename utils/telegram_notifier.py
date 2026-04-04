"""
Telegram Notifier per Polymarket Bot
=====================================
Notifiche async via Telegram Bot API per:
- Opportunità di arbitraggio trovate
- Trade eseguiti (live e paper)
- Report P&L periodici
- Errori critici
"""

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Rate limiting: max 20 messaggi/minuto (Telegram limit è 30)
_MAX_MESSAGES_PER_MINUTE = 20


class TelegramNotifier:

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = True,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.bot_token) and bool(self.chat_id)
        self._base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None
        self._msg_times: list[float] = []
        self._last_hourly_report = 0.0

        if not self.enabled:
            logger.warning(
                "[TELEGRAM] Disabilitato — TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti"
            )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _rate_ok(self) -> bool:
        now = time.time()
        self._msg_times = [t for t in self._msg_times if now - t < 60]
        return len(self._msg_times) < _MAX_MESSAGES_PER_MINUTE

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Invia un messaggio Telegram. Fallback a plain text se HTML parse fallisce."""
        if not self.enabled:
            return False

        if not self._rate_ok():
            logger.debug("[TELEGRAM] Rate limit raggiunto, messaggio saltato")
            return False

        try:
            session = await self._get_session()
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            async with session.post(
                f"{self._base_url}/sendMessage",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                self._msg_times.append(time.time())
                if resp.status == 200:
                    return True
                # v12.9: If HTML parse fails, retry without parse_mode
                if resp.status == 400 and parse_mode == "HTML":
                    import re
                    plain = re.sub(r'<[^>]+>', '', text)
                    payload_plain = {
                        "chat_id": self.chat_id,
                        "text": plain,
                        "disable_web_page_preview": True,
                    }
                    async with session.post(
                        f"{self._base_url}/sendMessage",
                        json=payload_plain,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp2:
                        if resp2.status == 200:
                            return True
                body = await resp.text()
                logger.warning(f"[TELEGRAM] Errore {resp.status}: {body[:200]}")
                return False
        except Exception as e:
            logger.warning(f"[TELEGRAM] Invio fallito: {e}")
            return False

    # ── Metodi specifici per tipo di notifica ──

    async def notify_arbitrage(
        self,
        opp_type: str,
        market_name: str,
        profit_per_dollar: float,
        total_cost: float,
        action: str,
        executed: bool = False,
    ):
        """Notifica opportunità di arbitraggio."""
        status = "✅ ESEGUITO" if executed else "🔍 TROVATO"
        emoji = "💰" if executed else "🎯"
        text = (
            f"{emoji} <b>ARB {opp_type.upper()} {status}</b>\n\n"
            f"📊 <b>{market_name[:80]}</b>\n"
            f"💵 Profitto/$ : <b>{profit_per_dollar:.2%}</b>\n"
            f"💰 Costo tot  : ${total_cost:.2f}\n"
            f"📋 Azione     : <code>{action[:120]}</code>"
        )
        await self.send(text)

    async def notify_trade(
        self,
        strategy: str,
        side: str,
        market_name: str,
        size: float,
        price: float,
        edge: float,
        paper: bool = True,
    ):
        """Notifica trade eseguito."""
        mode = "📝 PAPER" if paper else "🔴 LIVE"
        text = (
            f"{mode} <b>TRADE {strategy.upper()}</b>\n\n"
            f"📊 {market_name[:80]}\n"
            f"📈 {side} @ {price:.4f}\n"
            f"💵 Size: ${size:.2f} | Edge: {edge:.2%}"
        )
        await self.send(text)

    async def notify_resolution(
        self,
        market_name: str,
        won: bool,
        pnl: float,
        strategy: str,
    ):
        """Notifica risoluzione mercato."""
        emoji = "🟢" if won else "🔴"
        result = "VINTO" if won else "PERSO"
        text = (
            f"{emoji} <b>RISOLTO — {result}</b>\n\n"
            f"📊 {market_name[:80]}\n"
            f"💵 P/L: <b>${pnl:+.2f}</b>\n"
            f"📋 Strategia: {strategy}"
        )
        await self.send(text)

    async def notify_pnl_report(
        self,
        capital: float,
        daily_pnl: float,
        total_trades: int,
        win_rate: float,
        open_positions: int,
        usdc_balance: float,
        unrealized_pnl: float,
        strategy_pnl: dict[str, float],
        real_portfolio: dict = None,
        weekly_pnl: float = 0.0,
        alltime_pnl: float = 0.0,
        deposited: float = 6203.0,
    ):
        """Report P&L periodico (ogni ora). v12.10.8: sessione + settimanale + totale."""
        now = time.time()
        if now - self._last_hourly_report < 3600:
            return
        self._last_hourly_report = now

        # v12.10.8: solo strategie con PnL != 0 o attive
        active = {"mro_kelly", "btc_latency", "liquidity_vacuum", "weather", "sport_latency"}
        spnl_lines = []
        for k, v in sorted(strategy_pnl.items(), key=lambda x: -abs(x[1])):
            if v == 0 and k not in active:
                continue
            icon = "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")
            spnl_lines.append(f"  {icon} {k}: <b>${v:+.2f}</b>")
        spnl = "\n".join(spnl_lines) if spnl_lines else "  (nessun trade)"

        # PnL percentuali
        alltime_pct = (alltime_pnl / deposited * 100) if deposited > 0 else 0
        weekly_pct = (weekly_pnl / capital * 100) if capital > 0 else 0
        daily_pct = (daily_pnl / capital * 100) if capital > 0 else 0

        pnl_icon = "📈" if daily_pnl >= 0 else "📉"
        text = (
            f"{pnl_icon} <b>REPORT ORARIO</b>\n\n"
            f"💰 Capitale: <b>${capital:,.2f}</b>\n\n"
            f"📊 <b>PnL:</b>\n"
            f"  Sessione : <b>${daily_pnl:+.2f}</b> ({daily_pct:+.1f}%)\n"
            f"  7 giorni : <b>${weekly_pnl:+.2f}</b> ({weekly_pct:+.1f}%)\n"
            f"  Totale   : <b>${alltime_pnl:+.2f}</b> ({alltime_pct:+.1f}%)\n\n"
            f"💵 USDC: ${usdc_balance:,.2f} | 📦 Pos: {open_positions}\n"
            f"📋 Trades: {total_trades} (WR: {win_rate:.0f}%)\n\n"
            f"<b>Strategie:</b>\n{spnl}"
        )
        await self.send(text)

    async def notify_error(self, error_msg: str, strategy: str = ""):
        """Notifica errore critico."""
        text = (
            f"🚨 <b>ERRORE{' ' + strategy.upper() if strategy else ''}</b>\n\n"
            f"<code>{error_msg[:300]}</code>"
        )
        await self.send(text)

    async def notify_startup(self, mode: str, capital: float, strategies: list[str]):
        """Notifica avvio bot."""
        strat_list = "\n".join(f"  • {s}" for s in strategies)
        text = (
            f"🚀 <b>BOT AVVIATO — {mode}</b>\n\n"
            f"💰 Capitale: ${capital:,.2f}\n"
            f"📋 Strategie attive:\n{strat_list}"
        )
        await self.send(text)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
