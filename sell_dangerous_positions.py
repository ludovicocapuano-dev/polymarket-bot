"""
v12.10: Force exit di:
1. Tutte le posizioni imported_onchain (43 pos, $799 a rischio, 0% WR, 0 edge)
2. Posizioni weather pericolose dove forecast e' dentro/vicino al bucket
   (Dallas 86-87F, Seattle 56-57F, Miami 82-83F, Denver 76-79F, etc.)

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


# Citta' blacklistate in v12.10 — vendere tutte le posizioni weather su queste
BLACKLISTED_CITIES = {"houston", "los angeles", "denver", "dallas"}

# Mercati weather specifici dove forecast e' DENTRO il bucket (coin flip, non tail bet)
# Basato su analisi open_positions.json del 26 Mar 2026
DANGEROUS_WEATHER_KEYWORDS = [
    # Forecast dentro/vicino al bucket — non sono tail bets
    "86" , "87",    # Dallas 86-87F, forecast 86.9F
    "56", "57",     # Seattle 56-57F, forecast 56.5F
    "82", "83",     # Miami 82-83F, forecast 82.9F
    "76", "77",     # Denver 76-77F, forecast 79.9F
    "78", "79",     # Denver 78-79F, forecast 79.9F
]


def get_bid(token_id: str) -> float:
    try:
        r = requests.get(
            f"https://clob.polymarket.com/price?token_id={token_id}&side=SELL",
            timeout=5,
        )
        return float(r.json().get("price", 0))
    except Exception:
        return 0


def should_sell(pos: dict) -> tuple[bool, str]:
    """Decide se vendere la posizione e perche'."""
    strategy = pos.get("strategy", "")

    # 1. Tutte le imported_onchain
    if strategy == "imported_onchain":
        return True, "imported_onchain (0% WR, 0 edge)"

    # 2. Weather su citta' blacklistate
    if strategy == "weather":
        reason = pos.get("reason", "").lower()
        city = ""
        for c in BLACKLISTED_CITIES:
            if c in reason:
                city = c
                break
        if city:
            return True, f"weather blacklisted city: {city}"

    return False, ""


def main():
    config = Config.from_env()
    api = PolymarketAPI(config.creds)
    api.authenticate()

    if not api._authenticated:
        logger.error("Autenticazione fallita!")
        sys.exit(1)

    with open("logs/open_positions.json", "r") as f:
        positions = json.load(f)

    to_sell = []
    to_keep = []

    for p in positions:
        sell, reason = should_sell(p)
        if sell:
            p["_sell_reason"] = reason
            to_sell.append(p)
        else:
            to_keep.append(p)

    logger.info(f"Posizioni totali: {len(positions)} | Da vendere: {len(to_sell)} | Da tenere: {len(to_keep)}")

    if not to_sell:
        logger.info("Nessuna posizione da vendere!")
        return

    print()
    for i, pos in enumerate(to_sell, 1):
        logger.info(f"  [{i}] {pos.get('strategy'):20s} | {pos.get('side'):8s} @{pos.get('price', 0):.4f} | ${pos.get('size', 0):.2f} | {pos.get('_sell_reason')}")
    print()

    # Conferma
    confirm = input(f"Vendere {len(to_sell)} posizioni? [y/N] ").strip().lower()
    if confirm != 'y':
        logger.info("Annullato.")
        return

    total_recovered = 0
    total_invested = 0
    sold_count = 0
    failed = []

    for i, pos in enumerate(to_sell, 1):
        token_id = pos["token_id"]
        entry_price = pos.get("price", 0)
        size = pos.get("size", 0)
        reason = pos.get("_sell_reason", "")

        # Recupera shares reali dal CLOB
        fill_info = api.get_last_fill(token_id, side="BUY")
        if fill_info:
            shares = fill_info["fill_size"]
            logger.info(f"  [CLOB] Shares reali: {shares:.2f}")
        else:
            shares = size / entry_price if entry_price > 0 else 0
            logger.warning(f"  [CLOB] Fill info non disponibile, stima: {shares:.0f} shares")

        bid = get_bid(token_id)

        logger.info(
            f"[{i}/{len(to_sell)}] entry={entry_price:.4f} bid={bid:.4f} | "
            f"${size:.2f} ({shares:.2f} shares) | {reason}"
        )

        if bid <= 0 or shares <= 0:
            logger.warning(f"  -> SKIP: bid={bid} shares={shares} (illiquido)")
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
                logger.info(f"  -> VENDUTO! Recovery ${actual_recovery:.2f} | PnL: ${pnl:+.2f}")
            else:
                logger.warning(f"  -> FALLITO (result=None)")
                failed.append(pos)

        except Exception as e:
            logger.error(f"  -> ERRORE: {e}")
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
    print(f"FORCE EXIT v12.10 — DANGEROUS POSITIONS")
    print(f"  Vendute:    {sold_count}/{len(to_sell)}")
    print(f"  Fallite:    {len(failed)} (illiquide/errore)")
    print(f"  Investito:  ${total_invested:.2f}")
    print(f"  Recuperato: ~${total_recovered:.2f}")
    print(f"  PnL:        ${total_recovered - total_invested:+.2f}")
    print(f"  Posizioni rimaste: {len(remaining)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
