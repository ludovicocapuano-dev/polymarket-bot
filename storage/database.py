"""
PostgreSQL Storage v9.0 — Persistenza robusta per trade e metriche.

Schema:
- trades: storico completo trade con validation_score e brier_score
- market_snapshots: snapshot periodici prezzo/volume/liquidità
- calibration_log: log suggerimenti calibrazione
- drift_alerts: log alert concept drift
"""

import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class Database:
    """PostgreSQL storage con graceful degradation."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn = None
        self._available = False

    def connect(self) -> bool:
        """Connette a PostgreSQL. Ritorna False se non disponibile."""
        try:
            import psycopg2
            self._conn = psycopg2.connect(self.dsn)
            self._conn.autocommit = True
            self._create_tables()
            self._available = True
            logger.info("[DB] Connesso a PostgreSQL")
            return True
        except ImportError:
            logger.warning("[DB] psycopg2 non installato — storage DB disabilitato")
            return False
        except Exception as e:
            logger.warning(f"[DB] Connessione fallita: {e} — storage DB disabilitato")
            return False

    @property
    def available(self) -> bool:
        return self._available

    def _create_tables(self):
        """Crea le tabelle se non esistono."""
        with self._conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    strategy VARCHAR(50) NOT NULL,
                    signal_type VARCHAR(50),
                    category VARCHAR(100),
                    market_id VARCHAR(200) NOT NULL,
                    token_id VARCHAR(200),
                    side VARCHAR(10),
                    size NUMERIC(12,4),
                    price NUMERIC(8,6),
                    edge NUMERIC(8,6),
                    result VARCHAR(10) DEFAULT 'OPEN',
                    pnl NUMERIC(12,4) DEFAULT 0,
                    reason TEXT,
                    validation_score NUMERIC(4,3),
                    brier_score NUMERIC(6,4)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id SERIAL PRIMARY KEY,
                    market_id VARCHAR(200) NOT NULL,
                    price_yes NUMERIC(8,6),
                    price_no NUMERIC(8,6),
                    volume NUMERIC(16,2),
                    liquidity NUMERIC(16,2),
                    spread NUMERIC(8,6),
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS calibration_log (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    strategy VARCHAR(50),
                    parameter VARCHAR(100),
                    old_value VARCHAR(100),
                    new_value VARCHAR(100),
                    reason TEXT,
                    applied BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS drift_alerts (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    alert_type VARCHAR(50),
                    strategy VARCHAR(50),
                    severity VARCHAR(20),
                    message TEXT
                );
            """)
            # Indici per query comuni
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_market ON market_snapshots(market_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_drift_strategy ON drift_alerts(strategy);")
        logger.info("[DB] Tabelle create/verificate")

    def record_trade(self, strategy: str, market_id: str, token_id: str,
                     side: str, size: float, price: float, edge: float,
                     reason: str = "", signal_type: str = "",
                     category: str = "", validation_score: float = None):
        """Registra un nuovo trade."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO trades (strategy, signal_type, category, market_id,
                                       token_id, side, size, price, edge, reason,
                                       validation_score)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (strategy, signal_type, category, market_id, token_id,
                      side, size, price, edge, reason, validation_score))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.warning(f"[DB] Errore record_trade: {e}")

    def update_trade_result(self, market_id: str, token_id: str,
                            result: str, pnl: float, brier_score: float = None):
        """Aggiorna il risultato di un trade chiuso."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    UPDATE trades
                    SET result = %s, pnl = %s, brier_score = %s
                    WHERE market_id = %s AND token_id = %s AND result = 'OPEN'
                    ORDER BY timestamp DESC
                    LIMIT 1
                """, (result, pnl, brier_score, market_id, token_id))
        except Exception as e:
            logger.warning(f"[DB] Errore update_trade_result: {e}")

    def record_snapshot(self, market_id: str, price_yes: float, price_no: float,
                        volume: float, liquidity: float, spread: float):
        """Salva uno snapshot del mercato."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO market_snapshots (market_id, price_yes, price_no,
                                                  volume, liquidity, spread)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (market_id, price_yes, price_no, volume, liquidity, spread))
        except Exception as e:
            logger.warning(f"[DB] Errore record_snapshot: {e}")

    def record_calibration(self, strategy: str, parameter: str,
                           old_value: str, new_value: str, reason: str):
        """Logga un suggerimento di calibrazione."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO calibration_log (strategy, parameter, old_value,
                                                 new_value, reason)
                    VALUES (%s, %s, %s, %s, %s)
                """, (strategy, parameter, old_value, new_value, reason))
        except Exception as e:
            logger.warning(f"[DB] Errore record_calibration: {e}")

    def record_drift_alert(self, alert_type: str, strategy: str,
                           severity: str, message: str):
        """Logga un alert di drift."""
        if not self._available:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO drift_alerts (alert_type, strategy, severity, message)
                    VALUES (%s, %s, %s, %s)
                """, (alert_type, strategy, severity, message))
        except Exception as e:
            logger.warning(f"[DB] Errore record_drift_alert: {e}")

    def get_strategy_stats(self, strategy: str, days: int = 30) -> dict:
        """Ritorna statistiche per strategia negli ultimi N giorni."""
        if not self._available:
            return {}
        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
                        SUM(pnl) as total_pnl,
                        AVG(brier_score) as avg_brier
                    FROM trades
                    WHERE strategy = %s
                      AND timestamp > NOW() - INTERVAL '%s days'
                      AND result IN ('WIN', 'LOSS')
                """, (strategy, days))
                row = cur.fetchone()
                if row:
                    return {
                        "total": row[0] or 0,
                        "wins": row[1] or 0,
                        "losses": row[2] or 0,
                        "total_pnl": float(row[3] or 0),
                        "avg_brier": float(row[4]) if row[4] else None,
                    }
            return {}
        except Exception as e:
            logger.warning(f"[DB] Errore get_strategy_stats: {e}")
            return {}

    def close(self):
        """Chiude la connessione."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._available = False
