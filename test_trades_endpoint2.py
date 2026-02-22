"""Test: prova varianti dell'endpoint /trades per trovare il fill."""
import json
from config import Config
from utils.polymarket_api import PolymarketAPI

config = Config.from_env()
api = PolymarketAPI(config.creds)
api.authenticate()

from py_clob_client.headers.headers import create_level_2_headers
from py_clob_client.clob_types import RequestArgs

token = "30750011974272271774996473893784293456495448154641072807125114286195903027526"

# Variante 1: Senza next_cursor
request_args = RequestArgs(method="GET", request_path="/trades")
headers = create_level_2_headers(api.clob.signer, api.clob.creds, request_args)

print("=== Variante 1: asset_id senza cursor ===")
url1 = f"{config.creds.host}/trades?asset_id={token}"
resp = api._session.get(url1, headers=headers, timeout=5)
data = resp.json()
print(f"Count: {data.get('count', 'N/A')}, trades: {len(data.get('data', []))}")
if data.get("data"):
    for t in data["data"][:2]:
        print(f"  {t.get('side')} {t.get('size')} @{t.get('price')}")

# Variante 2: Tutti i trade recenti (senza filtro asset)
print("\n=== Variante 2: ultimi trade senza filtro asset ===")
url2 = f"{config.creds.host}/trades?next_cursor=MA=="
resp2 = api._session.get(url2, headers=headers, timeout=5)
data2 = resp2.json()
trades = data2.get("data", [])
print(f"Count: {data2.get('count', 'N/A')}, trades: {len(trades)}")
if trades:
    for t in trades[:5]:
        print(f"  asset={t.get('asset_id', 'N/A')[:20]}... side={t.get('side')} "
              f"size={t.get('size')} @{t.get('price')} time={t.get('match_time', 'N/A')}")

    # Cerco se il nostro token è tra i trade recenti
    found = [t for t in trades if t.get("asset_id") == token]
    print(f"\n  Trovati {len(found)} trade per il nostro token")

# Variante 3: Prova py_clob_client nativo get_trades
print("\n=== Variante 3: py_clob_client.get_trades() ===")
try:
    from py_clob_client.clob_types import TradeParams
    params = TradeParams(asset_id=token)
    result = api.clob.get_trades(params)
    print(f"Type: {type(result)}")
    if isinstance(result, list):
        print(f"Count: {len(result)}")
        for t in result[:3]:
            print(f"  {t}")
    elif isinstance(result, dict):
        print(f"Keys: {list(result.keys())}")
        trades = result.get("data", result.get("trades", []))
        print(f"Trades: {len(trades)}")
    else:
        print(f"Value: {str(result)[:300]}")
except Exception as e:
    print(f"Error: {e}")
