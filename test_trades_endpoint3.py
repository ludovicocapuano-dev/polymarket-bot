"""Test: analizza TUTTI i trade recenti per trovare pattern nei token mancanti."""
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

# Scarica tutti i trade (paginazione)
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

print(f"Totale trade scaricati: {len(all_trades)}")

# Tutti gli asset_id unici
asset_ids = set(t.get("asset_id", "") for t in all_trades)
print(f"Asset ID unici: {len(asset_ids)}")

# Token falliti dal log
failed_tokens = [
    "30750011974272271774996473893784293456495448154641072807125114286195903027526",
    "38147099293800302407880734555523686103928638640100945279020681171029084524244",
    "115001318889283190775511668585838292279254252087408314125234384188495396971331",
    "25925941224033898017062505399411451779004509082307072399163490623211159575696",
]

print("\n=== Ricerca token falliti ===")
for ft in failed_tokens:
    found = [t for t in all_trades if t.get("asset_id") == ft]
    prefix_match = [t for t in all_trades if t.get("asset_id", "").startswith(ft[:10])]
    print(f"  {ft[:20]}... exact={len(found)} prefix={len(prefix_match)}")

# Mostra gli asset_id reali per confronto
print("\n=== Top 15 asset_id per frequenza ===")
from collections import Counter
c = Counter(t.get("asset_id", "") for t in all_trades)
for aid, cnt in c.most_common(15):
    print(f"  {aid[:30]}... ({cnt} trades)")

# Controlla se il post_order ritorna info utili
# Guardiamo la struttura di un trade reale
print("\n=== Struttura trade di esempio ===")
if all_trades:
    print(json.dumps(all_trades[0], indent=2))
