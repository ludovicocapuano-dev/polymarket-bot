"""
Force exit delle 26 posizioni favorite_longshot legacy (pre-v12.5.2).
Edge reale 1.3-2.6% — troppo basso per giustificare il rischio.
Usa smart_sell (limit al bid, fallback market).
"""
import json
import sys
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)

from config import Config
from utils.polymarket_api import PolymarketAPI


def get_bid(token_id: str) -> float:
    try:
        r = requests.get(
            f"https://clob.polymarket.com/price?token_id={token_id}&side=SELL",
            timeout=5,
        )
        return float(r.json().get("price", 0))
    except Exception:
        return 0


def main():
    config = Config.from_env()
    api = PolymarketAPI(config.creds)
    api.authenticate()

    if not api._authenticated:
        logger.error("Autenticazione fallita!")
        sys.exit(1)

    with open("logs/open_positions.json", "r") as f:
        positions = json.load(f)

    EXIT_STRATEGIES = {"crowd_sport", "crowd_prediction"}
    to_sell = [p for p in positions if p.get("strategy") in EXIT_STRATEGIES]
    to_keep = [p for p in positions if p.get("strategy") not in EXIT_STRATEGIES]

    logger.info(f"Posizioni totali: {len(positions)} | Da vendere: {len(to_sell)} | Altre: {len(to_keep)}")
    print()

    total_recovered = 0
    total_invested = 0
    sold_count = 0
    failed = []

    for i, pos in enumerate(to_sell, 1):
        token_id = pos["token_id"]
        entry_price = pos["price"]
        size = pos["size"]
        reason = pos.get("reason", "")[:50]

        # Recupera shares reali dal CLOB
        fill_info = api.get_last_fill(token_id, side="BUY")
        if fill_info:
            shares = fill_info["fill_size"]
            logger.info(f"  [CLOB] Shares reali: {shares:.2f} (fill_price={fill_info['fill_price']:.4f})")
        else:
            shares = size / entry_price if entry_price > 0 else 0
            logger.warning(f"  [CLOB] Fill info non disponibile, stima: {shares:.0f} shares")

        bid = get_bid(token_id)

        logger.info(
            f"[{i}/{len(to_sell)}] entry={entry_price:.4f} bid={bid:.4f} | "
            f"${size:.2f} ({shares:.2f} shares) | {reason}"
        )

        if bid <= 0 or shares <= 0:
            logger.warning(f"  → SKIP: bid={bid} shares={shares}")
            failed.append(pos)
            continue

        est_recovery = shares * bid
        total_invested += size

        try:
            result = api.smart_sell(
                token_id=token_id,
                shares=shares,
                current_price=bid,
                timeout_sec=8.0,
                fallback_market=True,
            )

            if result:
                actual_recovery = est_recovery
                if isinstance(result, dict):
                    sell_fill = api.get_last_fill(token_id, side="SELL")
                    if sell_fill:
                        actual_recovery = sell_fill["fill_size"] * sell_fill["fill_price"]
                        logger.info(f"  [FILL] Recovery reale: ${actual_recovery:.2f}")

                total_recovered += actual_recovery
                sold_count += 1
                pnl = actual_recovery - size
                logger.info(f"  → VENDUTO! Recovery ${actual_recovery:.2f} | PnL: ${pnl:+.2f}")
            else:
                logger.warning(f"  → FALLITO (result=None)")
                failed.append(pos)

        except Exception as e:
            logger.error(f"  → ERRORE: {e}")
            failed.append(pos)

        time.sleep(1.5)

    # Aggiorna open_positions.json
    remaining = to_keep + failed
    with open("logs/open_positions.json", "w") as f:
        json.dump(
            [
                {k: p[k] for k in p if k in [
                    "timestamp", "strategy", "market_id", "token_id",
                    "side", "size", "price", "edge", "reason", "high_water_mark",
                ]}
                for p in remaining
            ],
            f,
            indent=2,
        )

    print()
    print("=" * 60)
    print(f"FORCE EXIT FAVORITE_LONGSHOT LEGACY")
    print(f"  Vendute:    {sold_count}/{len(to_sell)}")
    print(f"  Fallite:    {len(failed)}")
    print(f"  Investito:  ${total_invested:.2f}")
    print(f"  Recuperato: ~${total_recovered:.2f}")
    print(f"  PnL:        ${total_recovered - total_invested:+.2f}")
    print(f"  Posizioni rimaste: {len(remaining)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
