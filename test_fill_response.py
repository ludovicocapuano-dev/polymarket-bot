"""Test: cosa ritorna il CLOB dopo un ordine? Esamina la struttura della risposta."""
import json
import logging
logging.basicConfig(level=logging.WARNING)

from config import Config
from utils.polymarket_api import PolymarketAPI
from py_clob_client.clob_types import TradeParams

config = Config.from_env()
api = PolymarketAPI(config.creds)
api.authenticate()

# Prendi i primi 3 trade recenti per esaminare la struttura
import requests
headers = {}
from py_clob_client.headers.headers import create_level_2_headers
from py_clob_client.clob_types import RequestArgs

request_args = RequestArgs(method="GET", request_path="/trades")
hdrs = create_level_2_headers(api.clob.signer, api.clob.creds, request_args)

# Fetch solo prima pagina
url = f"{config.creds.host}/trades?next_cursor=MA=="
resp = requests.get(url, headers=hdrs)
data = resp.json()
print(f"Response keys: {data.keys()}")
print(f"Number of trades in first page: {len(data.get('data', []))}")
print(f"Next cursor: {data.get('next_cursor', 'N/A')}")

if data.get('data'):
    print(f"\nStruttura primo trade:")
    print(json.dumps(data['data'][0], indent=2))
    print(f"\nStruttura secondo trade:")
    print(json.dumps(data['data'][1], indent=2))

# Test con asset_id filter
if data.get('data'):
    asset_id = data['data'][0].get('asset_id', '')
    url2 = f"{config.creds.host}/trades?asset_id={asset_id}&next_cursor=MA=="
    resp2 = requests.get(url2, headers=hdrs)
    data2 = resp2.json()
    print(f"\nTrades per asset {asset_id[:20]}...: {len(data2.get('data', []))}")
