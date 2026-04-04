"""
ClickHouse Analytics — Storage analitico strutturato per trade whale e metriche.
================================================================================
Modulo ispirato alla pipeline ClickHouse di polybot per archiviazione e analisi
di trade, metriche profiler e label di ricerca.

Degradazione graziosa: funziona senza ClickHouse installato, identico al pattern
PostgreSQL in storage/database.py. Se clickhouse-connect non e' disponibile o
la connessione fallisce, tutte le operazioni diventano no-op silenziosi.

Schema:
- whale_trades: storico completo trade whale (Data API + Goldsky)
- whale_metrics: risultati profiler (WalletMetrics serializzate)
- research_labels: etichette di ricerca per indirizzi (tipo, valore, score)
- data_quality_daily: vista materializzata qualita' dati per giorno

Uso standalone:
    python3 -m utils.clickhouse_analytics              # test schema
    CLICKHOUSE_DSN=clickhouse://localhost python3 -m utils.clickhouse_analytics
"""

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Schema DDL ──────────────────────────────────────────────────────────────

SCHEMA_WHALE_TRADES = """
CREATE TABLE IF NOT EXISTS whale_trades (
    ts DateTime64(3),
    address String,
    name String,
    market_id String,
    side String,
    price Float64,
    size Float64,
    transaction_hash String DEFAULT '',
    token_id String DEFAULT '',
    source String DEFAULT 'data_api',
    question String DEFAULT '',
    ingested_at DateTime64(3) DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(ingested_at)
ORDER BY (address, market_id, ts, side)
"""

SCHEMA_WHALE_METRICS = """
CREATE TABLE IF NOT EXISTS whale_metrics (
    address String,
    name String,
    time_profitable_pct Float64,
    accumulation_pattern String,
    accumulation_score Float64,
    n_hedged_markets Int32,
    hedge_ratio Float64,
    avg_minutes_between_trades Float64,
    is_likely_bot UInt8,
    total_markets_analyzed Int32,
    total_trades_analyzed Int32,
    data_quality String,
    composite_score Float64,
    recommendation String,
    profiled_at DateTime64(3),
    ingested_at DateTime64(3) DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(ingested_at)
ORDER BY (address, profiled_at)
"""

SCHEMA_RESEARCH_LABELS = """
CREATE TABLE IF NOT EXISTS research_labels (
    address String,
    label_type String,
    label_value String,
    label_score Float64 DEFAULT 0.0,
    created_at DateTime64(3) DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(created_at)
ORDER BY (address, label_type, label_value)
"""

SCHEMA_DATA_QUALITY = """
CREATE MATERIALIZED VIEW IF NOT EXISTS data_quality_daily
ENGINE = SummingMergeTree()
ORDER BY (day, source)
POPULATE
AS
SELECT
    toDate(ts) AS day,
    source,
    count() AS n_trades,
    uniqExact(address) AS n_wallets,
    uniqExact(market_id) AS n_markets,
    avg(price) AS avg_price,
    sum(size) AS total_volume
FROM whale_trades
GROUP BY day, source
"""

_ALL_SCHEMAS = [
    ("whale_trades", SCHEMA_WHALE_TRADES),
    ("whale_metrics", SCHEMA_WHALE_METRICS),
    ("research_labels", SCHEMA_RESEARCH_LABELS),
    ("data_quality_daily", SCHEMA_DATA_QUALITY),
]


class ClickHouseAnalytics:
    """
    Client analitico ClickHouse con degradazione graziosa.

    Se il DSN non e' fornito o clickhouse-connect non e' installato,
    tutte le operazioni diventano no-op — nessuna eccezione propagata.
    Identico al pattern di storage/database.py per PostgreSQL.
    """

    def __init__(self, dsn: str | None = None):
        """
        Inizializza la connessione a ClickHouse.

        Parametri:
            dsn: stringa di connessione ClickHouse (es. 'clickhouse://localhost').
                 Se None, legge da env var CLICKHOUSE_DSN.
                 Se anche quella e' vuota, il modulo resta inattivo.
        """
        self._client: Any = None
        self._available = False
        resolved_dsn = dsn or os.getenv("CLICKHOUSE_DSN", "")
        if not resolved_dsn:
            logger.debug(
                "[CH] Nessun DSN ClickHouse fornito — analytics disabilitato"
            )
            return
        self._connect(resolved_dsn)

    def _connect(self, dsn: str) -> None:
        """Tenta la connessione a ClickHouse. Fallimento silenzioso."""
        try:
            import clickhouse_connect  # type: ignore[import-untyped]

            self._client = clickhouse_connect.get_client(dsn=dsn)
            # Ping di verifica: se il server non risponde, fallisce qui
            self._client.ping()
            self._available = True
            logger.info("[CH] Connesso a ClickHouse")
        except ImportError:
            logger.warning(
                "[CH] clickhouse-connect non installato — analytics disabilitato"
            )
        except Exception as e:
            logger.warning(
                f"[CH] Connessione fallita: {e} — analytics disabilitato"
            )

    def is_available(self) -> bool:
        """Ritorna True se la connessione a ClickHouse e' attiva."""
        return self._available

    # ── Schema ──────────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """
        Crea tutte le tabelle e viste materializzate.

        Esegue i DDL in ordine: prima le tabelle base, poi le viste
        che dipendono da esse. Sicuro da richiamare piu' volte
        (IF NOT EXISTS ovunque).
        """
        if not self._available:
            return
        for name, ddl in _ALL_SCHEMAS:
            try:
                self._client.command(ddl)
                logger.debug(f"[CH] Schema '{name}' creato/verificato")
            except Exception as e:
                logger.warning(f"[CH] Errore creazione schema '{name}': {e}")
        logger.info("[CH] Schema inizializzato")

    # ── Insert ──────────────────────────────────────────────────────────

    def insert_trades(
        self,
        address: str,
        name: str,
        trades: list[dict],
        source: str = "data_api",
    ) -> int:
        """
        Inserisce un batch di trade whale in ClickHouse.

        Parametri:
            address: indirizzo wallet (hex).
            name: nome leggibile del wallet.
            trades: lista di dict con chiavi {market_id, side, price, size,
                    timestamp, question?}. Formato identico a whale_profiler.
            source: sorgente dati ('data_api' o 'goldsky').

        Ritorna:
            Numero di righe inserite (0 se non disponibile).
        """
        if not self._available or not trades:
            return 0
        try:
            from datetime import datetime, timezone

            rows: list[list[Any]] = []
            for t in trades:
                ts_val = t.get("timestamp", 0)
                if isinstance(ts_val, (int, float)) and ts_val > 0:
                    dt = datetime.fromtimestamp(ts_val, tz=timezone.utc)
                else:
                    dt = datetime.now(timezone.utc)

                rows.append([
                    dt,                                      # ts
                    address,                                 # address
                    name,                                    # name
                    str(t.get("market_id", "")),             # market_id
                    str(t.get("side", "")),                  # side
                    float(t.get("price", 0)),                # price
                    float(t.get("size", 0)),                 # size
                    str(t.get("transaction_hash", "")),      # transaction_hash
                    str(t.get("token_id", "")),              # token_id
                    source,                                  # source
                    str(t.get("question", "")),              # question
                ])

            column_names = [
                "ts", "address", "name", "market_id", "side",
                "price", "size", "transaction_hash", "token_id",
                "source", "question",
            ]
            self._client.insert(
                "whale_trades",
                rows,
                column_names=column_names,
            )
            logger.debug(
                f"[CH] Inseriti {len(rows)} trade per {name} ({address[:10]}...)"
            )
            return len(rows)

        except Exception as e:
            logger.warning(f"[CH] Errore insert_trades: {e}")
            return 0

    def insert_metrics(self, metrics: Any) -> bool:
        """
        Inserisce il risultato del profiler (WalletMetrics) in ClickHouse.

        Parametri:
            metrics: istanza di WalletMetrics (da utils.whale_profiler).
                     Accetta qualsiasi oggetto con gli stessi attributi.

        Ritorna:
            True se inserito con successo, False altrimenti.
        """
        if not self._available:
            return False
        try:
            from datetime import datetime, timezone

            profiled_dt = datetime.fromtimestamp(
                getattr(metrics, "profiled_at", time.time()),
                tz=timezone.utc,
            )

            row = [[
                str(getattr(metrics, "address", "")),
                str(getattr(metrics, "name", "")),
                float(getattr(metrics, "time_profitable_pct", 0.0)),
                str(getattr(metrics, "accumulation_pattern", "UNKNOWN")),
                float(getattr(metrics, "accumulation_score", 0.0)),
                int(getattr(metrics, "n_hedged_markets", 0)),
                float(getattr(metrics, "hedge_ratio", 0.0)),
                float(getattr(metrics, "avg_minutes_between_trades", 0.0)),
                1 if getattr(metrics, "is_likely_bot", False) else 0,
                int(getattr(metrics, "total_markets_analyzed", 0)),
                int(getattr(metrics, "total_trades_analyzed", 0)),
                str(getattr(metrics, "data_quality", "INSUFFICIENT")),
                float(getattr(metrics, "composite_score", 0.0)),
                str(getattr(metrics, "recommendation", "SKIP")),
                profiled_dt,
            ]]

            column_names = [
                "address", "name", "time_profitable_pct",
                "accumulation_pattern", "accumulation_score",
                "n_hedged_markets", "hedge_ratio",
                "avg_minutes_between_trades", "is_likely_bot",
                "total_markets_analyzed", "total_trades_analyzed",
                "data_quality", "composite_score", "recommendation",
                "profiled_at",
            ]
            self._client.insert(
                "whale_metrics",
                row,
                column_names=column_names,
            )
            logger.debug(
                f"[CH] Metriche inserite per "
                f"{getattr(metrics, 'name', '?')} "
                f"(score={getattr(metrics, 'composite_score', 0):.2f})"
            )
            return True

        except Exception as e:
            logger.warning(f"[CH] Errore insert_metrics: {e}")
            return False

    def insert_label(
        self,
        address: str,
        label_type: str,
        label_value: str,
        label_score: float = 0.0,
    ) -> bool:
        """
        Inserisce una label di ricerca per un indirizzo.

        Parametri:
            address: indirizzo wallet (hex).
            label_type: tipo di etichetta (es. 'whale_tier', 'strategy', 'cluster').
            label_value: valore dell'etichetta (es. 'mega_whale', 'informed_trader').
            label_score: punteggio opzionale 0.0-1.0.

        Ritorna:
            True se inserito con successo, False altrimenti.
        """
        if not self._available:
            return False
        try:
            self._client.insert(
                "research_labels",
                [[address, label_type, label_value, label_score]],
                column_names=["address", "label_type", "label_value", "label_score"],
            )
            logger.debug(
                f"[CH] Label inserita: {address[:10]}... "
                f"{label_type}={label_value} (score={label_score:.2f})"
            )
            return True
        except Exception as e:
            logger.warning(f"[CH] Errore insert_label: {e}")
            return False

    # ── Query ───────────────────────────────────────────────────────────

    def query_trades(
        self, address: str, since_ts: float = 0
    ) -> list[dict]:
        """
        Recupera i trade archiviati per un indirizzo.

        Parametri:
            address: indirizzo wallet (hex).
            since_ts: timestamp Unix minimo (default 0 = tutti).

        Ritorna:
            Lista di dict con le colonne della tabella whale_trades.
            Lista vuota se non disponibile o nessun risultato.
        """
        if not self._available:
            return []
        try:
            from datetime import datetime, timezone

            if since_ts > 0:
                since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
                query = (
                    "SELECT ts, address, name, market_id, side, price, size, "
                    "       transaction_hash, token_id, source, question "
                    "FROM whale_trades "
                    "WHERE address = {addr:String} AND ts >= {since:DateTime64(3)} "
                    "ORDER BY ts"
                )
                result = self._client.query(
                    query,
                    parameters={"addr": address, "since": since_dt},
                )
            else:
                query = (
                    "SELECT ts, address, name, market_id, side, price, size, "
                    "       transaction_hash, token_id, source, question "
                    "FROM whale_trades "
                    "WHERE address = {addr:String} "
                    "ORDER BY ts"
                )
                result = self._client.query(
                    query,
                    parameters={"addr": address},
                )

            columns = [
                "ts", "address", "name", "market_id", "side", "price",
                "size", "transaction_hash", "token_id", "source", "question",
            ]
            rows = []
            for row in result.result_rows:
                rows.append(dict(zip(columns, row)))
            return rows

        except Exception as e:
            logger.warning(f"[CH] Errore query_trades: {e}")
            return []

    def query_data_quality(self, days: int = 7) -> list[dict]:
        """
        Report qualita' dati dalla vista materializzata.

        Parametri:
            days: numero di giorni da analizzare (default 7).

        Ritorna:
            Lista di dict con colonne {day, source, n_trades, n_wallets,
            n_markets, avg_price, total_volume}. Lista vuota se non disponibile.
        """
        if not self._available:
            return []
        try:
            query = (
                "SELECT day, source, n_trades, n_wallets, n_markets, "
                "       avg_price, total_volume "
                "FROM data_quality_daily "
                "WHERE day >= today() - {days:UInt32} "
                "ORDER BY day DESC, source"
            )
            result = self._client.query(
                query,
                parameters={"days": days},
            )
            columns = [
                "day", "source", "n_trades", "n_wallets",
                "n_markets", "avg_price", "total_volume",
            ]
            rows = []
            for row in result.result_rows:
                rows.append(dict(zip(columns, row)))
            return rows

        except Exception as e:
            logger.warning(f"[CH] Errore query_data_quality: {e}")
            return []

    def query_trade_count(self, address: str) -> int:
        """
        Conta il numero totale di trade archiviati per un indirizzo.

        Parametri:
            address: indirizzo wallet (hex).

        Ritorna:
            Conteggio trade (0 se non disponibile).
        """
        if not self._available:
            return 0
        try:
            query = (
                "SELECT count() "
                "FROM whale_trades "
                "WHERE address = {addr:String}"
            )
            result = self._client.query(
                query,
                parameters={"addr": address},
            )
            if result.result_rows:
                return int(result.result_rows[0][0])
            return 0
        except Exception as e:
            logger.warning(f"[CH] Errore query_trade_count: {e}")
            return 0


class ClickHouseWriter:
    """
    Writer batch leggero per inserimento trade in ClickHouse.

    Accumula trade in un buffer interno e li scrive in batch quando
    il buffer raggiunge la soglia (default 1000 record) o quando
    flush() viene invocato manualmente.

    Degradazione graziosa: se analytics non e' disponibile, il buffer
    si svuota silenziosamente al flush senza errori.
    """

    FLUSH_THRESHOLD = 1000

    def __init__(self, analytics: ClickHouseAnalytics):
        """
        Inizializza il writer batch.

        Parametri:
            analytics: istanza di ClickHouseAnalytics (puo' essere inattiva).
        """
        self._analytics = analytics
        self._buffer: list[tuple[str, str, dict, str]] = []

    def buffer_trade(
        self,
        address: str,
        name: str,
        trade: dict,
        source: str = "data_api",
    ) -> None:
        """
        Aggiunge un singolo trade al buffer.

        Se il buffer raggiunge FLUSH_THRESHOLD (1000), esegue
        automaticamente il flush verso ClickHouse.

        Parametri:
            address: indirizzo wallet (hex).
            name: nome leggibile del wallet.
            trade: dict con chiavi {market_id, side, price, size, timestamp, ...}.
            source: sorgente dati ('data_api' o 'goldsky').
        """
        self._buffer.append((address, name, trade, source))
        if len(self._buffer) >= self.FLUSH_THRESHOLD:
            self.flush()

    def flush(self) -> int:
        """
        Scrive tutti i trade bufferizzati in ClickHouse.

        Raggruppa per (address, name, source) per inserimenti batch
        efficienti. Svuota il buffer anche se l'inserimento fallisce
        (per evitare crescita illimitata della memoria).

        Ritorna:
            Numero totale di righe inserite.
        """
        if not self._buffer:
            return 0

        # Raggruppa per (address, name, source) per batch insert efficienti
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for address, name, trade, source in self._buffer:
            key = (address, name, source)
            groups.setdefault(key, []).append(trade)

        # Svuota il buffer PRIMA dell'inserimento (evita crescita se errore)
        buffer_size = len(self._buffer)
        self._buffer.clear()

        if not self._analytics.is_available():
            logger.debug(
                f"[CH-WRITER] Flush {buffer_size} trade scartati "
                f"(ClickHouse non disponibile)"
            )
            return 0

        total_inserted = 0
        for (address, name, source), trades in groups.items():
            inserted = self._analytics.insert_trades(
                address, name, trades, source
            )
            total_inserted += inserted

        if total_inserted > 0:
            logger.debug(
                f"[CH-WRITER] Flush completato: {total_inserted}/{buffer_size} "
                f"trade inseriti"
            )

        return total_inserted

    @property
    def buffer_size(self) -> int:
        """Numero di trade attualmente nel buffer."""
        return len(self._buffer)


# ── Main: test schema ───────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    )

    dsn = os.getenv("CLICKHOUSE_DSN", "")
    print("=" * 70)
    print("  ClickHouse Analytics — Test Schema")
    print("=" * 70)

    if not dsn:
        print(
            "\n  CLICKHOUSE_DSN non impostato."
            "\n  Verifica degradazione graziosa (tutte le operazioni no-op).\n"
        )

    analytics = ClickHouseAnalytics(dsn=dsn or None)

    print(f"  Disponibile: {analytics.is_available()}")

    # Test schema creation (no-op se non connesso)
    analytics.init_schema()

    # Test insert (no-op se non connesso)
    dummy_trades = [
        {
            "market_id": "test_market_001",
            "side": "YES",
            "price": 0.65,
            "size": 100.0,
            "timestamp": time.time(),
            "question": "Test market?",
        },
        {
            "market_id": "test_market_001",
            "side": "NO",
            "price": 0.35,
            "size": 50.0,
            "timestamp": time.time(),
            "question": "Test market?",
        },
    ]
    n_inserted = analytics.insert_trades(
        address="0xTEST_ADDRESS",
        name="test_wallet",
        trades=dummy_trades,
        source="data_api",
    )
    print(f"  Trade inseriti: {n_inserted}")

    # Test label insert
    label_ok = analytics.insert_label(
        address="0xTEST_ADDRESS",
        label_type="whale_tier",
        label_value="mega_whale",
        label_score=0.95,
    )
    print(f"  Label inserita: {label_ok}")

    # Test query
    stored = analytics.query_trades("0xTEST_ADDRESS")
    print(f"  Trade recuperati: {len(stored)}")

    count = analytics.query_trade_count("0xTEST_ADDRESS")
    print(f"  Trade count: {count}")

    quality = analytics.query_data_quality(days=7)
    print(f"  Report qualita' (righe): {len(quality)}")

    # Test writer batch
    writer = ClickHouseWriter(analytics)
    for i in range(5):
        writer.buffer_trade(
            address="0xTEST_WRITER",
            name="writer_test",
            trade={
                "market_id": f"market_{i:03d}",
                "side": "YES",
                "price": 0.50 + i * 0.05,
                "size": 10.0 * (i + 1),
                "timestamp": time.time(),
            },
            source="data_api",
        )
    print(f"  Buffer size prima del flush: {writer.buffer_size}")
    flushed = writer.flush()
    print(f"  Trade flushati: {flushed}")
    print(f"  Buffer size dopo il flush: {writer.buffer_size}")

    print("\n" + "=" * 70)
    if analytics.is_available():
        print("  Tutti i test completati con successo (ClickHouse attivo).")
    else:
        print(
            "  Degradazione graziosa verificata (nessun errore senza ClickHouse)."
        )
    print("=" * 70 + "\n")
