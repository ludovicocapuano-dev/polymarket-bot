"""
Market Database — Structured SQLite storage for Polymarket data (v12.8)
=======================================================================
Replaces scattered JSON files with a queryable database.

Four-layer pipeline:
1. REST API → upsert_market(), record trades
2. WebSocket → record_price_snapshot() real-time
3. Storage → SQLite WAL, indexed, queryable
4. Processing → get_price_history(), analytics queries

Based on the AleiahLock data pipeline pattern, adapted for our bot.
"""

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import logging

logger = logging.getLogger(__name__)

DB_PATH = "logs/polymarket.db"


class MarketDatabase:
    """SQLite storage for Polymarket data."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._create_tables()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _create_tables(self):
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS markets (
                    market_id       TEXT PRIMARY KEY,
                    condition_id    TEXT,
                    question        TEXT NOT NULL,
                    category        TEXT,
                    description     TEXT,
                    active          INTEGER,
                    closed          INTEGER,
                    resolved        INTEGER,
                    volume          REAL DEFAULT 0,
                    liquidity       REAL DEFAULT 0,
                    end_date        TEXT,
                    winner          TEXT,
                    neg_risk        INTEGER DEFAULT 0,
                    created_at      TEXT,
                    updated_at      TEXT
                );

                CREATE TABLE IF NOT EXISTS outcome_tokens (
                    token_id        TEXT PRIMARY KEY,
                    market_id       TEXT NOT NULL,
                    outcome         TEXT NOT NULL,
                    FOREIGN KEY (market_id) REFERENCES markets(market_id)
                );

                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id        TEXT NOT NULL,
                    market_id       TEXT NOT NULL,
                    outcome         TEXT NOT NULL,
                    price           REAL NOT NULL,
                    best_bid        REAL,
                    -- v12.8: unique constraint prevents duplicate snapshots on reconnect
                    best_ask        REAL,
                    spread          REAL,
                    snapshot_time   TEXT NOT NULL,
                    source          TEXT DEFAULT 'api'
                );

                CREATE TABLE IF NOT EXISTS bot_trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy        TEXT NOT NULL,
                    market_id       TEXT NOT NULL,
                    token_id        TEXT NOT NULL,
                    side            TEXT NOT NULL,
                    price           REAL NOT NULL,
                    size            REAL NOT NULL,
                    edge            REAL DEFAULT 0,
                    confidence      REAL DEFAULT 0,
                    reason          TEXT,
                    result          TEXT DEFAULT 'OPEN',
                    pnl             REAL DEFAULT 0,
                    open_time       TEXT NOT NULL,
                    close_time      TEXT,
                    close_reason    TEXT
                );

                CREATE TABLE IF NOT EXISTS market_trades (
                    trade_id        TEXT,
                    market_id       TEXT NOT NULL,
                    token_id        TEXT NOT NULL,
                    outcome         TEXT,
                    side            TEXT NOT NULL,
                    price           REAL NOT NULL,
                    size            REAL NOT NULL,
                    trade_time      TEXT NOT NULL,
                    UNIQUE(trade_id)
                );

                CREATE TABLE IF NOT EXISTS market_state_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id       TEXT NOT NULL,
                    old_state       TEXT,
                    new_state       TEXT NOT NULL,
                    changed_at      TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_state_history_market
                    ON market_state_history(market_id, changed_at);

                CREATE INDEX IF NOT EXISTS idx_market_trades_token_time
                    ON market_trades(token_id, trade_time);
                CREATE INDEX IF NOT EXISTS idx_market_trades_market_time
                    ON market_trades(market_id, trade_time);

                CREATE TABLE IF NOT EXISTS crowd_predictions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain          TEXT NOT NULL,
                    market_id       TEXT NOT NULL,
                    question        TEXT NOT NULL,
                    crowd_prob      REAL NOT NULL,
                    market_price    REAL NOT NULL,
                    edge            REAL NOT NULL,
                    side            TEXT NOT NULL,
                    confidence      REAL NOT NULL,
                    agent_count     INTEGER DEFAULT 50,
                    research_quality TEXT,
                    prediction_time TEXT NOT NULL,
                    resolved        INTEGER DEFAULT 0,
                    actual_outcome  TEXT
                );

                CREATE TABLE IF NOT EXISTS uw_signals (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    source          TEXT NOT NULL,
                    ticker          TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    strength        REAL NOT NULL,
                    detail          TEXT,
                    matched_market  TEXT,
                    signal_time     TEXT NOT NULL
                );

                -- v12.8: Idempotency — prevent duplicate snapshots on WS reconnect
                -- Round snapshot_time to second precision for dedup
                CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_dedup
                    ON price_snapshots(token_id, snapshot_time);
                CREATE INDEX IF NOT EXISTS idx_snapshots_token_time
                    ON price_snapshots(token_id, snapshot_time);
                CREATE INDEX IF NOT EXISTS idx_snapshots_market_time
                    ON price_snapshots(market_id, snapshot_time);
                CREATE INDEX IF NOT EXISTS idx_trades_strategy
                    ON bot_trades(strategy, open_time);
                CREATE INDEX IF NOT EXISTS idx_trades_result
                    ON bot_trades(result);
                CREATE INDEX IF NOT EXISTS idx_crowd_domain
                    ON crowd_predictions(domain, prediction_time);
                CREATE INDEX IF NOT EXISTS idx_uw_source
                    ON uw_signals(source, signal_time);
            """)

    # ── Markets ───────────────────────────────────────────────

    def _market_state(self, active: bool, closed: bool, resolved: bool) -> str:
        if resolved:
            return "resolved"
        if closed:
            return "closed"
        if active:
            return "active"
        return "archived"

    def upsert_market(self, market_id: str, condition_id: str, question: str,
                      category: str = "", volume: float = 0, liquidity: float = 0,
                      active: bool = True, closed: bool = False, resolved: bool = False,
                      end_date: str = "", neg_risk: bool = False, **kwargs):
        now = datetime.now(timezone.utc).isoformat()
        new_state = self._market_state(active, closed, resolved)

        with self.connection() as conn:
            # Check current state for transition tracking
            row = conn.execute(
                "SELECT active, closed, resolved FROM markets WHERE market_id = ?",
                (market_id,)
            ).fetchone()

            if row:
                old_state = self._market_state(bool(row["active"]), bool(row["closed"]), bool(row["resolved"]))
                if old_state != new_state:
                    conn.execute("""
                        INSERT INTO market_state_history (market_id, old_state, new_state, changed_at)
                        VALUES (?, ?, ?, ?)
                    """, (market_id, old_state, new_state, now))

            conn.execute("""
                INSERT INTO markets
                    (market_id, condition_id, question, category,
                     active, closed, resolved, volume, liquidity,
                     end_date, neg_risk, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    active=excluded.active, closed=excluded.closed,
                    resolved=excluded.resolved, volume=excluded.volume,
                    liquidity=excluded.liquidity, updated_at=excluded.updated_at
            """, (market_id, condition_id, question, category,
                  int(active), int(closed), int(resolved), volume, liquidity,
                  end_date, int(neg_risk), now, now))

    def upsert_token(self, token_id: str, market_id: str, outcome: str):
        with self.connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO outcome_tokens (token_id, market_id, outcome)
                VALUES (?, ?, ?)
            """, (token_id, market_id, outcome))

    # ── Price Snapshots ───────────────────────────────────────

    def record_price(self, token_id: str, market_id: str, outcome: str,
                     price: float, bid: float = None, ask: float = None,
                     source: str = "api"):
        """Record a price snapshot. Idempotent — duplicates are silently ignored."""
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        # Round to second precision for dedup (same token + same second = duplicate)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with self.connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO price_snapshots
                    (token_id, market_id, outcome, price, best_bid, best_ask, spread, snapshot_time, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (token_id, market_id, outcome, price, bid, ask, spread, ts, source))

    def get_price_history(self, market_id: str, hours: int = 24) -> list[dict]:
        with self.connection() as conn:
            cutoff = datetime.fromtimestamp(
                time.time() - hours * 3600, tz=timezone.utc
            ).isoformat()
            rows = conn.execute("""
                SELECT token_id, outcome, price, best_bid, best_ask, spread, snapshot_time
                FROM price_snapshots WHERE market_id = ? AND snapshot_time > ?
                ORDER BY snapshot_time ASC
            """, (market_id, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def get_latest_prices(self, market_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT ps.* FROM price_snapshots ps
                INNER JOIN (
                    SELECT token_id, MAX(snapshot_time) as max_time
                    FROM price_snapshots WHERE market_id = ? GROUP BY token_id
                ) latest ON ps.token_id = latest.token_id AND ps.snapshot_time = latest.max_time
                WHERE ps.market_id = ?
            """, (market_id, market_id)).fetchall()
            return [dict(r) for r in rows]

    # ── Bot Trades ────────────────────────────────────────────

    def record_trade(self, strategy: str, market_id: str, token_id: str,
                     side: str, price: float, size: float,
                     edge: float = 0, confidence: float = 0, reason: str = ""):
        with self.connection() as conn:
            conn.execute("""
                INSERT INTO bot_trades
                    (strategy, market_id, token_id, side, price, size,
                     edge, confidence, reason, result, open_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """, (strategy, market_id, token_id, side, price, size,
                  edge, confidence, reason, datetime.now(timezone.utc).isoformat()))

    def close_trade(self, token_id: str, result: str, pnl: float, close_reason: str = ""):
        with self.connection() as conn:
            conn.execute("""
                UPDATE bot_trades SET result=?, pnl=?, close_time=?, close_reason=?
                WHERE token_id=? AND result='OPEN'
            """, (result, pnl, datetime.now(timezone.utc).isoformat(), close_reason, token_id))

    def get_strategy_stats(self, strategy: str = None, days: int = 7) -> dict:
        with self.connection() as conn:
            cutoff = datetime.fromtimestamp(
                time.time() - days * 86400, tz=timezone.utc
            ).isoformat()
            where = "WHERE open_time > ?"
            params = [cutoff]
            if strategy:
                where += " AND strategy = ?"
                params.append(strategy)

            rows = conn.execute(f"""
                SELECT strategy, result, COUNT(*) as cnt, SUM(pnl) as total_pnl
                FROM bot_trades {where}
                GROUP BY strategy, result
            """, params).fetchall()

            stats = {}
            for r in rows:
                s = r["strategy"]
                if s not in stats:
                    stats[s] = {"wins": 0, "losses": 0, "open": 0, "pnl": 0}
                if r["result"] == "WIN":
                    stats[s]["wins"] = r["cnt"]
                    stats[s]["pnl"] += r["total_pnl"] or 0
                elif r["result"] == "LOSS":
                    stats[s]["losses"] = r["cnt"]
                    stats[s]["pnl"] += r["total_pnl"] or 0
                else:
                    stats[s]["open"] = r["cnt"]
            return stats

    # ── Market Trades (all trades, not just ours) ──────────────

    def record_market_trade(self, trade_id: str, market_id: str, token_id: str,
                            side: str, price: float, size: float,
                            trade_time: str = "", outcome: str = ""):
        """Record a market trade. Idempotent via UNIQUE(trade_id)."""
        if not trade_time:
            trade_time = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO market_trades
                    (trade_id, market_id, token_id, outcome, side, price, size, trade_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (trade_id, market_id, token_id, outcome, side, price, size, trade_time))

    def get_market_vwap(self, token_id: str, hours: int = 1) -> Optional[float]:
        """Calculate VWAP for a token over the last N hours."""
        with self.connection() as conn:
            cutoff = datetime.fromtimestamp(
                time.time() - hours * 3600, tz=timezone.utc
            ).isoformat()
            row = conn.execute("""
                SELECT SUM(price * size) / SUM(size) as vwap, SUM(size) as total_volume
                FROM market_trades WHERE token_id = ? AND trade_time > ?
            """, (token_id, cutoff)).fetchone()
            if row and row["vwap"]:
                return float(row["vwap"])
            return None

    def get_volume_imbalance(self, token_id: str, hours: int = 1) -> Optional[float]:
        """Buy volume / total volume ratio. >0.5 = net buying, <0.5 = net selling."""
        with self.connection() as conn:
            cutoff = datetime.fromtimestamp(
                time.time() - hours * 3600, tz=timezone.utc
            ).isoformat()
            row = conn.execute("""
                SELECT
                    SUM(CASE WHEN side='BUY' THEN size ELSE 0 END) as buy_vol,
                    SUM(size) as total_vol
                FROM market_trades WHERE token_id = ? AND trade_time > ?
            """, (token_id, cutoff)).fetchone()
            if row and row["total_vol"] and row["total_vol"] > 0:
                return float(row["buy_vol"] or 0) / float(row["total_vol"])
            return None

    # ── Crowd Predictions ─────────────────────────────────────

    def record_prediction(self, domain: str, market_id: str, question: str,
                          crowd_prob: float, market_price: float, edge: float,
                          side: str, confidence: float, agent_count: int = 50,
                          research_quality: str = ""):
        with self.connection() as conn:
            conn.execute("""
                INSERT INTO crowd_predictions
                    (domain, market_id, question, crowd_prob, market_price,
                     edge, side, confidence, agent_count, research_quality, prediction_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (domain, market_id, question, crowd_prob, market_price,
                  edge, side, confidence, agent_count, research_quality,
                  datetime.now(timezone.utc).isoformat()))

    def get_prediction_accuracy(self, domain: str = None) -> dict:
        with self.connection() as conn:
            where = "WHERE resolved = 1"
            params = []
            if domain:
                where += " AND domain = ?"
                params.append(domain)
            rows = conn.execute(f"""
                SELECT domain, COUNT(*) as total,
                    SUM(CASE WHEN (side='BUY_YES' AND actual_outcome='YES')
                              OR (side='BUY_NO' AND actual_outcome='NO')
                         THEN 1 ELSE 0 END) as correct
                FROM crowd_predictions {where}
                GROUP BY domain
            """, params).fetchall()
            return {r["domain"]: {"total": r["total"], "correct": r["correct"],
                                   "accuracy": r["correct"] / r["total"] if r["total"] > 0 else 0}
                    for r in rows}

    # ── UW Signals ────────────────────────────────────────────

    def record_uw_signal(self, source: str, ticker: str, direction: str,
                         strength: float, detail: str = "", matched_market: str = ""):
        with self.connection() as conn:
            conn.execute("""
                INSERT INTO uw_signals
                    (source, ticker, direction, strength, detail, matched_market, signal_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (source, ticker, direction, strength, detail, matched_market,
                  datetime.now(timezone.utc).isoformat()))

    # ── Queries ───────────────────────────────────────────────

    def get_liquid_markets(self, min_volume: float = 5000,
                          max_spread: float = 0.10,
                          min_book_depth: float = 50) -> list[dict]:
        """Get markets with REAL liquidity (not just Gamma's approximation).
        Uses actual spread data and trade volume from our snapshots."""
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT m.market_id, m.question, m.volume, m.liquidity,
                    AVG(ps.spread) as avg_spread,
                    COUNT(ps.id) as snapshot_count
                FROM markets m
                LEFT JOIN price_snapshots ps ON m.market_id = ps.market_id
                WHERE m.active = 1 AND m.closed = 0 AND m.volume >= ?
                GROUP BY m.market_id
                HAVING avg_spread IS NULL OR avg_spread <= ?
                ORDER BY m.volume DESC
            """, (min_volume, max_spread)).fetchall()
            return [dict(r) for r in rows]

    def get_active_markets(self, min_liquidity: float = 1000) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT m.*, t.token_id, t.outcome
                FROM markets m
                JOIN outcome_tokens t ON m.market_id = t.market_id
                WHERE m.active = 1 AND m.closed = 0 AND m.liquidity >= ?
                ORDER BY m.liquidity DESC
            """, (min_liquidity,)).fetchall()
            return [dict(r) for r in rows]

    def get_db_stats(self) -> dict:
        with self.connection() as conn:
            markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
            snapshots = conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
            trades = conn.execute("SELECT COUNT(*) FROM bot_trades").fetchone()[0]
            predictions = conn.execute("SELECT COUNT(*) FROM crowd_predictions").fetchone()[0]
            signals = conn.execute("SELECT COUNT(*) FROM uw_signals").fetchone()[0]
            return {
                "markets": markets,
                "price_snapshots": snapshots,
                "bot_trades": trades,
                "crowd_predictions": predictions,
                "uw_signals": signals,
            }

    def market_summary(self, market_id: str) -> dict:
        """Get summary of a market's current state."""
        latest = self.get_latest_prices(market_id)
        history = self.get_price_history(market_id, hours=24)

        result = {"market_id": market_id, "prices": [], "price_change_24h": 0}
        for row in latest:
            result["prices"].append({
                "outcome": row["outcome"],
                "price": row["price"],
                "spread": row.get("spread"),
            })

        if history:
            yes_prices = [h["price"] for h in history if h.get("outcome", "").lower() == "yes"]
            if len(yes_prices) > 1:
                result["yes_range"] = (min(yes_prices), max(yes_prices))
                result["price_change_24h"] = yes_prices[-1] - yes_prices[0]

        return result

    def find_moving_markets(self, hours: int = 1, min_move: float = 0.05) -> list[dict]:
        """Find markets where price moved significantly — spots incoming information."""
        with self.connection() as conn:
            rows = conn.execute("""
                WITH recent AS (
                    SELECT market_id, outcome, price, snapshot_time,
                        ROW_NUMBER() OVER (PARTITION BY market_id, outcome ORDER BY snapshot_time DESC) as rn_latest,
                        ROW_NUMBER() OVER (PARTITION BY market_id, outcome ORDER BY snapshot_time ASC) as rn_earliest
                    FROM price_snapshots
                    WHERE snapshot_time > datetime('now', ?)
                      AND outcome = 'Yes'
                ),
                pivoted AS (
                    SELECT market_id,
                        MAX(CASE WHEN rn_latest = 1 THEN price END) as latest_price,
                        MAX(CASE WHEN rn_earliest = 1 THEN price END) as first_price
                    FROM recent GROUP BY market_id
                )
                SELECT p.market_id, m.question, p.first_price, p.latest_price,
                    ABS(p.latest_price - p.first_price) as price_move
                FROM pivoted p
                JOIN markets m ON p.market_id = m.market_id
                WHERE ABS(p.latest_price - p.first_price) >= ?
                ORDER BY price_move DESC LIMIT 20
            """, (f"-{hours} hours", min_move)).fetchall()
            return [dict(r) for r in rows]

    def get_pre_resolution_prices(self, hours_before: int = 24) -> list[dict]:
        """Get prices just before markets resolved — for calibration studies."""
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT h.market_id, m.question, m.winner,
                    h.changed_at as resolved_at,
                    ps.price as pre_resolution_price,
                    ps.outcome,
                    ps.snapshot_time
                FROM market_state_history h
                JOIN markets m ON h.market_id = m.market_id
                LEFT JOIN price_snapshots ps ON h.market_id = ps.market_id
                    AND ps.snapshot_time < h.changed_at
                    AND ps.snapshot_time > datetime(h.changed_at, ?)
                WHERE h.new_state = 'resolved'
                ORDER BY h.changed_at DESC, ps.snapshot_time DESC
            """, (f"-{hours_before} hours",)).fetchall()
            return [dict(r) for r in rows]

    def get_state_transitions(self, market_id: str = None) -> list[dict]:
        """Get state transition history for a market or all markets."""
        with self.connection() as conn:
            if market_id:
                rows = conn.execute("""
                    SELECT h.*, m.question FROM market_state_history h
                    JOIN markets m ON h.market_id = m.market_id
                    WHERE h.market_id = ? ORDER BY h.changed_at
                """, (market_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT h.*, m.question FROM market_state_history h
                    JOIN markets m ON h.market_id = m.market_id
                    ORDER BY h.changed_at DESC LIMIT 50
                """).fetchall()
            return [dict(r) for r in rows]

    def cleanup_old_snapshots(self, keep_days: int = 7):
        """Remove price snapshots older than N days to save disk."""
        with self.connection() as conn:
            cutoff = datetime.fromtimestamp(
                time.time() - keep_days * 86400, tz=timezone.utc
            ).isoformat()
            result = conn.execute(
                "DELETE FROM price_snapshots WHERE snapshot_time < ?", (cutoff,)
            )
            logger.info(f"[DB] Cleaned {result.rowcount} old snapshots")


# Global instance
db = MarketDatabase()
