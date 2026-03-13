"""
Script vendita posizioni irrecuperabili (<$1 valore corrente).
Fetcha posizioni on-chain, filtra quelle con currentValue < $1, vende via smart_sell.
"""
import json
import os
import sys
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)

from config import Config
from utils.polymarket_api import PolymarketAPI
from dotenv import load_dotenv

VALUE_THRESHOLD = 1.0  # vendi tutto sotto $1


def get_onchain_positions(funder: str):
    resp = requests.get(
        "https://data-api.polymarket.com/positions",
        params={"user": funder, "sizeThreshold": "0"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    load_dotenv()
    config = Config.from_env()
    api = PolymarketAPI(config.creds)
    api.authenticate()

    if not api._authenticated:
        logger.error("Autenticazione fallita!")
        sys.exit(1)

    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS") or os.getenv("FUNDER_ADDRESS") or os.getenv("PROXY_ADDRESS")
    if not funder:
        logger.error("Nessun funder address trovato in .env")
        sys.exit(1)

    positions = get_onchain_positions(funder)
    irrecoverable = [
        p for p in positions
        if p.get("currentValue", 0) < VALUE_THRESHOLD
        and not p.get("redeemable", False)
        and p.get("size", 0) > 0
    ]

    logger.info(f"Posizioni totali on-chain: {len(positions)}")
    logger.info(f"Irrecuperabili (<${VALUE_THRESHOLD}): {len(irrecoverable)}")

    if not irrecoverable:
        logger.info("Nessuna posizione irrecuperabile da vendere.")
        return

    # Preview
    total_value = 0
    for p in irrecoverable:
        title = (p.get("title") or "?")[:60]
        val = p.get("currentValue", 0)
        size = p.get("size", 0)
        outcome = p.get("outcome", "?")
        token_id = p.get("asset", "")
        total_value += val
        logger.info(f"  ${val:.2f} | {outcome} {size:.1f}sh | {title}")

    logger.info(f"Valore totale recuperabile: ~${total_value:.2f}")
    print()

    sold_count = 0
    failed_count = 0
    total_recovered = 0

    for i, p in enumerate(irrecoverable, 1):
        token_id = p.get("asset", "")
        size = p.get("size", 0)
        outcome = p.get("outcome", "?")
        title = (p.get("title") or "?")[:50]
        current_value = p.get("currentValue", 0)

        if not token_id or size <= 0:
            logger.warning(f"[{i}/{len(irrecoverable)}] SKIP: no token_id or 0 shares | {title}")
            failed_count += 1
            continue

        # Get actual on-chain balance
        balance = api.get_token_balance(token_id)
        if balance <= 0:
            logger.info(f"[{i}/{len(irrecoverable)}] SKIP: 0 shares on-chain | {title}")
            continue

        # Get current bid
        try:
            r = requests.get(
                f"https://clob.polymarket.com/price?token_id={token_id}&side=SELL",
                timeout=5,
            )
            bid = float(r.json().get("price", 0))
        except Exception:
            bid = 0

        est_recovery = balance * bid if bid > 0 else 0
        logger.info(
            f"[{i}/{len(irrecoverable)}] {outcome} {balance:.1f}sh bid={bid:.3f} "
            f"~${est_recovery:.2f} | {title}"
        )

        if bid <= 0.001:
            logger.warning(f"  -> SKIP: bid troppo basso ({bid})")
            failed_count += 1
            continue

        try:
            result = api.smart_sell(
                token_id=token_id,
                shares=balance,
                current_price=bid,
                timeout_sec=8.0,
                fallback_market=True,
                aggressive=True,
            )

            if result:
                total_recovered += est_recovery
                sold_count += 1
                logger.info(f"  -> VENDUTO ~${est_recovery:.2f}")
            else:
                logger.warning(f"  -> FALLITO (result=None)")
                failed_count += 1

        except Exception as e:
            logger.error(f"  -> ERRORE: {e}")
            failed_count += 1

        time.sleep(1.0)

    print()
    print("=" * 60)
    print(f"RISULTATO VENDITA IRRECUPERABILI")
    print(f"  Vendute:    {sold_count}/{len(irrecoverable)}")
    print(f"  Fallite:    {failed_count}")
    print(f"  Skip (0sh): {len(irrecoverable) - sold_count - failed_count}")
    print(f"  Recuperato: ~${total_recovered:.2f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
