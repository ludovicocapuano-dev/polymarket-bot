"""Test: trova il fill usando maker_address nel maker_orders."""
import json
from config import Config
from utils.polymarket_api import PolymarketAPI

config = Config.from_env()
api = PolymarketAPI(config.creds)
api.authenticate()

from py_clob_client.headers.headers import create_level_2_headers
from py_clob_client.clob_types import RequestArgs

request_args = RequestArgs(method="GET", request_path="/trades")
headers = create_level_2_headers(api.clob.signer, api.clob.creds, request_args)

# Scarica tutti i trade
all_trades = []
cursor = "MA=="
for page in range(5):
    url = f"{config.creds.host}/trades?next_cursor={cursor}"
    resp = api._session.get(url, headers=headers, timeout=5)
    data = resp.json()
    trades = data.get("data", [])
    all_trades.extend(trades)
    cursor = data.get("next_cursor", "")
    if not trades or cursor == "LTE=":
        break

print(f"Totale trade: {len(all_trades)}")

# Il nostro wallet
our_wallet = "0x22051C507402383e696B408D3971df2989698089"
token_fail = "30750011974272271774996473893784293456495448154641072807125114286195903027526"

# Cerca trade dove siamo TAKER diretto (asset_id match)
taker_direct = [t for t in all_trades if t.get("asset_id") == token_fail]
print(f"\nTaker diretto per token_fail: {len(taker_direct)}")

# Cerca trade dove siamo MAKER (nei maker_orders)
maker_matches = []
for t in all_trades:
    for mo in t.get("maker_orders", []):
        if mo.get("asset_id") == token_fail:
            maker_matches.append({
                "trade_id": t["id"],
                "trade_asset": t["asset_id"][:20],
                "our_side": mo.get("side"),
                "our_price": mo.get("price"),
                "our_size": mo.get("matched_amount"),
                "outcome": mo.get("outcome"),
                "match_time": t.get("match_time"),
                "trader_side": t.get("trader_side"),
            })

print(f"Maker match per token_fail: {len(maker_matches)}")
for m in maker_matches[:5]:
    print(f"  {json.dumps(m)}")

# Analisi generale: quanti trade siamo taker vs maker?
our_as_taker = [t for t in all_trades if t.get("trader_side") == "TAKER"]
our_as_maker = [t for t in all_trades if t.get("trader_side") == "MAKER"]
print(f"\nTotale: taker={len(our_as_taker)} maker={len(our_as_maker)} altro={len(all_trades)-len(our_as_taker)-len(our_as_maker)}")

# Per i trade come TAKER, il nostro token è nell'asset_id principale
# Per i trade come MAKER, il nostro token è nei maker_orders
# Verifichiamo
print("\n=== Come estrarre fill per entrambi i casi ===")
# Trade come TAKER: usiamo price e size direttamente
if our_as_taker:
    t = our_as_taker[0]
    print(f"TAKER: asset={t['asset_id'][:20]}... price={t['price']} size={t['size']} side={t['side']}")

# Trade come MAKER: usiamo maker_orders[nostro].price e matched_amount
if our_as_maker:
    t = our_as_maker[0]
    for mo in t.get("maker_orders", []):
        if mo.get("maker_address") == our_wallet:
            print(f"MAKER: asset={mo['asset_id'][:20]}... price={mo['price']} size={mo['matched_amount']} side={mo['side']} outcome={mo['outcome']}")
            break
