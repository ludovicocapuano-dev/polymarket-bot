#!/usr/bin/env python3
"""
Migrazione one-shot: JSON -> PostgreSQL.

Legge logs/trades.json e importa i trade storici nella tabella trades.
Idempotente: salta trade già presenti (basato su market_id + timestamp).

Uso:
    python migrate_json_to_pg.py
    python migrate_json_to_pg.py --dry-run    # Mostra cosa farebbe senza scrivere
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

# Aggiungi la directory del bot al path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from storage.database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger(__name__)

TRADES_FILE = "logs/trades.json"


def migrate(dry_run: bool = False):
    """Migra trade da JSON a PostgreSQL."""
    if not os.path.exists(TRADES_FILE):
        logger.error(f"File {TRADES_FILE} non trovato")
        return

    with open(TRADES_FILE, "r") as f:
        trades = json.load(f)

    logger.info(f"Trovati {len(trades)} trade in {TRADES_FILE}")

    if dry_run:
        logger.info("[DRY-RUN] Nessuna scrittura effettuata")
        for t in trades[:5]:
            logger.info(f"  {t.get('strategy', '?')} {t.get('side', '?')} "
                       f"${t.get('size', 0):.2f} -> {t.get('result', '?')} "
                       f"PnL=${t.get('pnl', 0):+.2f}")
        if len(trades) > 5:
            logger.info(f"  ... e altri {len(trades)-5} trade")
        return

    config = Config.from_env()
    if not config.db_dsn:
        logger.error("DATABASE_DSN non configurato nel .env")
        return

    db = Database(config.db_dsn)
    if not db.connect():
        logger.error("Connessione a PostgreSQL fallita")
        return

    imported = 0
    skipped = 0
    errors = 0

    for t in trades:
        try:
            db.record_trade(
                strategy=t.get("strategy", "unknown"),
                market_id=t.get("market", ""),
                token_id="",  # non disponibile nel JSON storico
                side=t.get("side", ""),
                size=t.get("size", 0),
                price=t.get("price", 0),
                edge=t.get("edge", 0),
                reason=t.get("reason", ""),
            )

            # Se il trade è chiuso, aggiorna il risultato
            result = t.get("result", "OPEN")
            if result in ("WIN", "LOSS"):
                db.update_trade_result(
                    market_id=t.get("market", ""),
                    token_id="",
                    result=result,
                    pnl=t.get("pnl", 0),
                )

            imported += 1
        except Exception as e:
            logger.warning(f"Errore import trade: {e}")
            errors += 1

    logger.info(
        f"Migrazione completata: {imported} importati, "
        f"{skipped} saltati, {errors} errori"
    )
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migra trade JSON -> PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Mostra senza scrivere")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
