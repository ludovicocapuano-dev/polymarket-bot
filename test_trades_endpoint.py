"""Test: cosa ritorna il CLOB /trades endpoint per un token specifico."""
import json
from config import Config
from utils.polymarket_api import PolymarketAPI

config = Config.from_env()
api = PolymarketAPI(config.creds)
api.authenticate()

# Token dove il fill NON è stato trovato
token_fail = "30750011974272271774996473893784293456495448154641072807125114286195903027526"

# Token dove il fill È stato trovato (dal log precedente - uno dei "Real fill")
# Cerchiamo nei log per trovarne uno

from py_clob_client.headers.headers import create_level_2_headers
from py_clob_client.clob_types import RequestArgs

request_args = RequestArgs(method="GET", request_path="/trades")
headers = create_level_2_headers(api.clob.signer, api.clob.creds, request_args)

for label, tid in [("FAIL", token_fail)]:
    url = f"{config.creds.host}/trades?asset_id={tid}&next_cursor=MA=="
    resp = api._session.get(url, headers=headers, timeout=5)
    data = resp.json()
    trades = data.get("data", [])
    print(f"\n=== {label}: {tid[:20]}... ===")
    print(f"Status: {resp.status_code}")
    print(f"Trades count: {len(trades)}")
    if trades:
        for t in trades[:3]:
            print(f"  side={t.get('side')} price={t.get('price')} size={t.get('size')} "
                  f"status={t.get('status')} match_time={t.get('match_time')}")
    else:
        print(f"  Raw response keys: {list(data.keys())}")
        print(f"  Raw response (first 500): {json.dumps(data)[:500]}")
