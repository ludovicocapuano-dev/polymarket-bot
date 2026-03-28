"""
Rich-based terminal dashboard for the Polymarket trading bot.

Displays real-time P&L, win rate, open positions, capital, recent trades,
strategy breakdown, and status alerts in a 4-panel grid layout.

Activation:
  - CLI flag: --dashboard
  - Env var: DASHBOARD=1
  - Only activates on interactive terminals (sys.stdout.isatty())

Graceful degradation: if `rich` is not installed, logs a warning and disables.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel: attempt to import rich at module level for type hints only.
# Actual availability is checked in __post_init__ so the module can always
# be imported without side effects.
# ---------------------------------------------------------------------------
_RICH_AVAILABLE = False
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    _RICH_AVAILABLE = True
except ImportError:
    pass


BOT_VERSION = "v12.10"


@dataclass
class _TradeRecord:
    """Lightweight record kept in the trades ring-buffer."""
    timestamp: float
    strategy: str
    side: str
    size: float
    price: float
    edge: float
    won: Optional[bool]
    pnl: float
    market_name: str


@dataclass
class Dashboard:
    """Non-blocking Rich terminal dashboard for the Polymarket bot.

    Designed to run as an ``asyncio.Task`` alongside the main bot loop
    via ``asyncio.gather``.  All public mutator methods are plain (sync)
    calls so the bot can update state without awaiting anything.
    """

    _disabled: bool = False
    _trades_history: deque = field(default_factory=lambda: deque(maxlen=50))
    _daily_pnl: float = 0.0
    _total_pnl: float = 0.0
    _wins: int = 0
    _losses: int = 0
    _capital: float = 0.0
    _open_positions: int = 0
    _strategies_active: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _start_time: float = field(default_factory=time.monotonic)
    _status_lines: deque = field(default_factory=lambda: deque(maxlen=8))
    _pid: int = field(default_factory=os.getpid)

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        """Try to import rich; disable gracefully if unavailable or non-TTY."""
        if not _RICH_AVAILABLE:
            logger.warning(
                "dashboard: 'rich' library not installed — dashboard disabled. "
                "Install with: pip install rich"
            )
            self._disabled = True
            return

        if not sys.stdout.isatty():
            logger.info(
                "dashboard: stdout is not a TTY (nohup / redirect?) — dashboard disabled."
            )
            self._disabled = True
            return

        # Seed a default status line so the panel is never empty.
        self._status_lines.append(f"Bot running (PID {self._pid})")

    # ------------------------------------------------------------------
    # Public mutators (called from the bot, sync)
    # ------------------------------------------------------------------

    def record_trade(
        self,
        strategy: str,
        side: str,
        size: float,
        price: float,
        edge: float,
        won: Optional[bool],
        pnl: float,
        market_name: str = "",
    ) -> None:
        """Record a completed (or freshly placed) trade.

        Parameters
        ----------
        strategy : str
            Strategy name, e.g. ``"weather"``, ``"resolution_sniper"``.
        side : str
            ``"BUY YES"`` or ``"BUY NO"``.
        size : float
            Dollar size of the trade.
        price : float
            Execution price (0-1).
        edge : float
            Estimated edge at entry (0-1).
        won : bool | None
            ``True`` = win, ``False`` = loss, ``None`` = still open.
        pnl : float
            Realised P&L for this trade (0.0 if still open).
        market_name : str
            Human-readable market slug / question (truncated in display).
        """
        if self._disabled:
            return

        rec = _TradeRecord(
            timestamp=time.time(),
            strategy=strategy,
            side=side,
            size=size,
            price=price,
            edge=edge,
            won=won,
            pnl=pnl,
            market_name=market_name,
        )
        self._trades_history.append(rec)

        # Update win/loss counters.
        if won is True:
            self._wins += 1
        elif won is False:
            self._losses += 1

        # Update daily + total P&L.
        self._daily_pnl += pnl
        self._total_pnl += pnl

        # Update per-strategy stats.
        s = self._strategies_active.setdefault(
            strategy, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        )
        s["trades"] += 1
        if won is True:
            s["wins"] += 1
        elif won is False:
            s["losses"] += 1
        s["pnl"] += pnl

    def update_capital(self, capital: float, open_positions: int) -> None:
        """Update the capital and open-positions display."""
        if self._disabled:
            return
        self._capital = capital
        self._open_positions = open_positions

    def add_status(self, line: str) -> None:
        """Push a status/alert line (most recent at top)."""
        if self._disabled:
            return
        self._status_lines.append(line)

    def reset_daily(self) -> None:
        """Reset daily P&L counters (call at midnight UTC)."""
        self._daily_pnl = 0.0

    # ------------------------------------------------------------------
    # Async run loop
    # ------------------------------------------------------------------

    async def run(self, refresh_interval: float = 5.0) -> None:
        """Main dashboard loop — meant to be passed to ``asyncio.gather``.

        Creates a Rich ``Live`` context and refreshes the layout every
        *refresh_interval* seconds.  The loop yields control via
        ``asyncio.sleep`` so it never blocks the bot.
        """
        if self._disabled:
            logger.info("dashboard: disabled — run() is a no-op.")
            # Keep the coroutine alive so gather() doesn't exit.
            while True:
                await asyncio.sleep(3600)

        console = Console()
        with Live(
            self._build_layout(),
            console=console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            while True:
                await asyncio.sleep(refresh_interval)
                try:
                    live.update(self._build_layout())
                except Exception:
                    # Never let a rendering glitch crash the bot.
                    logger.debug("dashboard: render error", exc_info=True)

    # ------------------------------------------------------------------
    # Layout builder
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        """Assemble the 4-panel Rich layout."""
        layout = Layout()

        layout.split_column(
            Layout(name="top", ratio=2),
            Layout(name="bottom", ratio=3),
        )
        layout["top"].split_row(
            Layout(name="overview", ratio=1),
            Layout(name="strategies", ratio=1),
        )
        layout["bottom"].split_row(
            Layout(name="trades", ratio=3),
            Layout(name="status", ratio=1),
        )

        layout["overview"].update(self._panel_overview())
        layout["strategies"].update(self._panel_strategies())
        layout["trades"].update(self._panel_trades())
        layout["status"].update(self._panel_status())

        return layout

    # ------------------------------------------------------------------
    # Individual panels
    # ------------------------------------------------------------------

    def _panel_overview(self) -> Panel:
        """Panel 1: Overview — capital, P&L, win rate, uptime."""
        total_trades = self._wins + self._losses
        wr = (self._wins / total_trades * 100) if total_trades > 0 else 0.0
        daily_pct = (
            (self._daily_pnl / self._capital * 100) if self._capital > 0 else 0.0
        )

        # Uptime.
        elapsed = time.monotonic() - self._start_time
        hours, rem = divmod(int(elapsed), 3600)
        minutes, _ = divmod(rem, 60)

        # Colour helpers.
        def _pnl_colour(val: float) -> str:
            if val > 0:
                return f"[green]+${val:,.2f}[/green]"
            elif val < 0:
                return f"[red]-${abs(val):,.2f}[/red]"
            return f"$0.00"

        lines = (
            f"  Capital:    [bold]${self._capital:,.2f}[/bold]\n"
            f"  Daily PnL:  {_pnl_colour(self._daily_pnl)} ({daily_pct:+.1f}%)\n"
            f"  Total PnL:  {_pnl_colour(self._total_pnl)}\n"
            f"  Win Rate:   [bold]{wr:.1f}%[/bold] ({self._wins}W/{self._losses}L)\n"
            f"  Open Pos:   {self._open_positions}\n"
            f"  Uptime:     {hours}h {minutes:02d}m"
        )
        return Panel(
            Text.from_markup(lines),
            title=f"POLYMARKET BOT {BOT_VERSION}",
            border_style="bright_blue",
        )

    def _panel_strategies(self) -> Panel:
        """Panel 2: Per-strategy breakdown table."""
        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=None,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("Strategy", style="bold", min_width=18)
        table.add_column("Trades", justify="right", min_width=6)
        table.add_column("WR", justify="right", min_width=5)
        table.add_column("PnL", justify="right", min_width=8)

        # Sort by number of trades descending; show all known strategies.
        sorted_strats = sorted(
            self._strategies_active.items(),
            key=lambda kv: kv[1]["trades"],
            reverse=True,
        )

        for name, s in sorted_strats:
            t = s["trades"]
            w = s["wins"]
            wr_str = f"{w / t * 100:.0f}%" if t > 0 else "-"
            pnl_val = s["pnl"]
            if pnl_val > 0:
                pnl_str = f"[green]+${pnl_val:,.0f}[/green]"
            elif pnl_val < 0:
                pnl_str = f"[red]-${abs(pnl_val):,.0f}[/red]"
            else:
                pnl_str = "$0"
            table.add_row(name, str(t), wr_str, pnl_str)

        if not sorted_strats:
            table.add_row("[dim]waiting for trades...[/dim]", "", "", "")

        return Panel(table, title="STRATEGIES", border_style="bright_cyan")

    def _panel_trades(self) -> Panel:
        """Panel 3: Last 10 trades table."""
        table = Table(
            show_header=True,
            header_style="bold yellow",
            box=None,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("Time", min_width=6)
        table.add_column("Strategy", min_width=16)
        table.add_column("Side", min_width=9)
        table.add_column("Size", justify="right", min_width=6)
        table.add_column("Price", justify="right", min_width=6)
        table.add_column("Edge", justify="right", min_width=6)
        table.add_column("PnL", justify="right", min_width=7)

        # Show most recent 10, newest first.
        recent = list(self._trades_history)[-10:]
        recent.reverse()

        for rec in recent:
            ts = datetime.fromtimestamp(rec.timestamp, tz=timezone.utc)
            time_str = ts.strftime("%H:%M")
            pnl_val = rec.pnl
            if pnl_val > 0:
                pnl_str = f"[green]+${pnl_val:,.0f}[/green]"
            elif pnl_val < 0:
                pnl_str = f"[red]-${abs(pnl_val):,.0f}[/red]"
            else:
                pnl_str = "[dim]$0[/dim]"

            table.add_row(
                time_str,
                rec.strategy,
                rec.side,
                f"${rec.size:,.0f}",
                f"{rec.price:.3f}",
                f"{rec.edge * 100:.1f}%",
                pnl_str,
            )

        if not recent:
            table.add_row(
                "[dim]--[/dim]",
                "[dim]waiting for trades...[/dim]",
                "", "", "", "", "",
            )

        return Panel(table, title="RECENT TRADES", border_style="bright_yellow")

    def _panel_status(self) -> Panel:
        """Panel 4: Alerts / status lines."""
        # Build status text from deque (newest last, display newest first).
        lines_list = list(self._status_lines)
        lines_list.reverse()

        rendered_lines = []
        for line in lines_list[:8]:
            rendered_lines.append(f"  {line}")

        text = "\n".join(rendered_lines) if rendered_lines else "  [dim]No alerts[/dim]"
        return Panel(
            Text.from_markup(text),
            title="STATUS",
            border_style="bright_magenta",
        )


# ---------------------------------------------------------------------------
# Helper: check whether the dashboard should be enabled.
# ---------------------------------------------------------------------------

def should_enable_dashboard() -> bool:
    """Return True if the dashboard should be activated.

    Checks (all must pass):
      1. ``--dashboard`` in sys.argv  OR  ``DASHBOARD=1`` env var.
      2. stdout is a TTY (not nohup / pipe / redirect).
    """
    flag_requested = (
        "--dashboard" in sys.argv
        or os.environ.get("DASHBOARD", "0") == "1"
    )
    return flag_requested and sys.stdout.isatty()
