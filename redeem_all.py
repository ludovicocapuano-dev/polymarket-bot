#!/usr/bin/env python3
"""
v10.3: Script per riscuotere TUTTE le posizioni redeemable on-chain.

Bypassa il matching con open_trades — redime direttamente tutte le
posizioni che la Data API segna come redeemable.

Uso: python3 redeem_all.py [--dry-run]
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

from config import Config
from utils.redeemer import Redeemer


def main():
    dry_run = "--dry-run" in sys.argv

    config = Config.from_env()
    priv_key = config.creds.private_key.strip()
    funder = config.creds.funder_address.strip() if config.creds.funder_address else ""

    if not funder:
        logger.error("FUNDER_ADDRESS non configurato in .env")
        sys.exit(1)

    redeemer = Redeemer(priv_key, funder)
    if not redeemer.available:
        logger.error("Redeemer non disponibile (web3 non connesso)")
        sys.exit(1)

    # Mostra posizioni redeemable
    positions = redeemer.fetch_redeemable_positions()
    if not positions:
        logger.info("Nessuna posizione redeemable trovata")
        return

    logger.info(f"Trovate {len(positions)} posizioni redeemable")
    for i, pos in enumerate(positions):
        cid = pos.get("conditionId", "") or pos.get("condition_id", "")
        title = pos.get("title", "") or pos.get("question", "") or "?"
        size = pos.get("size", "?")
        logger.info(f"  [{i+1}] {title[:60]} (cond={cid[:16]}... size={size})")

    if dry_run:
        logger.info("[DRY-RUN] Nessun redeem eseguito")
        return

    # Conferma
    print(f"\nRedeem {len(positions)} posizioni? Digita 'REDEEM' per confermare: ", end="")
    confirm = input().strip()
    if confirm != "REDEEM":
        logger.info("Annullato")
        return

    # Esegui
    stats = redeemer.redeem_all_redeemable()
    logger.info(f"Risultato: {stats}")


if __name__ == "__main__":
    main()
