"""
Script vendita manuale — Vende le 14 posizioni crypto_5min + data_driven longshot.
Usa smart_sell (limit al bid, fallback market).
Aggiorna open_positions.json dopo ogni vendita.
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

# Token da vendere: crypto_5min YES a prezzo basso + data_driven YES a prezzo basso
SELL_CRITERIA = [
    # (strategy, side, max_entry_price)
    ("crypto_5min", "BUY_YES", 0.05),
    ("data_driven", "BUY_YES", 0.15),
]


def should_sell(pos: dict) -> bool:
    for strat, side, max_price in SELL_CRITERIA:
        if pos["strategy"] == strat and pos["side"] == side and pos["price"] < max_price:
            return True
    return False


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

    # Carica posizioni aperte
    with open("logs/open_positions.json", "r") as f:
        positions = json.load(f)

    to_sell = [p for p in positions if should_sell(p)]
    to_keep = [p for p in positions if not should_sell(p)]

    logger.info(f"Posizioni totali: {len(positions)} | Da vendere: {len(to_sell)} | Da tenere: {len(to_keep)}")
    print()

    total_recovered = 0
    total_invested = 0
    sold_count = 0
    failed = []

    for i, pos in enumerate(to_sell, 1):
        token_id = pos["token_id"]
        entry_price = pos["price"]
        size = pos["size"]
        strategy = pos["strategy"]
        reason = pos.get("reason", "")[:50]

        # v7.4: Recupera shares REALI dal CLOB invece di calcolare size/price
        fill_info = api.get_last_fill(token_id, side="BUY")
        if fill_info:
            shares = fill_info["fill_size"]
            logger.info(f"  [CLOB] Shares reali: {shares:.2f} (fill_price={fill_info['fill_price']:.4f})")
        else:
            shares = size / entry_price if entry_price > 0 else 0
            logger.warning(f"  [CLOB] Fill info non disponibile, stima: {shares:.0f} shares")

        # Prezzo bid attuale
        bid = get_bid(token_id)

        logger.info(
            f"[{i}/{len(to_sell)}] {strategy} | entry={entry_price:.4f} bid={bid:.4f} | "
            f"${size:.2f} ({shares:.2f} shares) | {reason}"
        )

        if bid <= 0 or shares <= 0:
            logger.warning(f"  → SKIP: bid={bid} shares={shares}")
            failed.append(pos)
            continue

        # Stima recovery con shares REALI
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
                # v7.4: Verifica recovery reale dal CLOB
                actual_recovery = est_recovery
                if isinstance(result, dict):
                    sell_fill = api.get_last_fill(token_id, side="SELL")
                    if sell_fill:
                        actual_recovery = sell_fill["fill_size"] * sell_fill["fill_price"]
                        logger.info(f"  [FILL] Recovery reale: ${actual_recovery:.2f}")

                total_recovered += actual_recovery
                sold_count += 1
                pnl = actual_recovery - size
                logger.info(
                    f"  → VENDUTO! Recovery ${actual_recovery:.2f} | PnL: ${pnl:+.2f}"
                )
            else:
                logger.warning(f"  → FALLITO (result=None)")
                failed.append(pos)

        except Exception as e:
            logger.error(f"  → ERRORE: {e}")
            failed.append(pos)

        time.sleep(1.5)  # rate limit

    # Aggiorna open_positions.json — rimuovi le vendute, tieni le failed
    remaining = to_keep + failed
    with open("logs/open_positions.json", "w") as f:
        json.dump(
            [
                {
                    "timestamp": p["timestamp"],
                    "strategy": p["strategy"],
                    "market_id": p["market_id"],
                    "token_id": p["token_id"],
                    "side": p["side"],
                    "size": p["size"],
                    "price": p["price"],
                    "edge": p["edge"],
                    "reason": p.get("reason", ""),
                }
                for p in remaining
            ],
            f,
            indent=2,
        )

    print()
    print("=" * 60)
    print(f"RISULTATO VENDITA")
    print(f"  Vendute:    {sold_count}/{len(to_sell)}")
    print(f"  Fallite:    {len(failed)}")
    print(f"  Investito:  ${total_invested:.2f}")
    print(f"  Recuperato: ~${total_recovered:.2f}")
    print(f"  PnL:        ${total_recovered - total_invested:+.2f}")
    print(f"  Posizioni rimaste: {len(remaining)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
