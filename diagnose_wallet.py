"""
Diagnostica wallet — controlla bilancio USDC, ordini aperti e trade recenti.
"""
import json
import sys
import logging

logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)-8s | %(message)s")

from config import Config
from utils.polymarket_api import PolymarketAPI
from py_clob_client.clob_types import OpenOrderParams, TradeParams

def main():
    config = Config.from_env()
    api = PolymarketAPI(config.creds)
    api.authenticate()

    if not api._authenticated:
        print("ERRORE: Autenticazione fallita!")
        sys.exit(1)

    print("=" * 70)
    print("DIAGNOSTICA WALLET POLYMARKET")
    print("=" * 70)

    # 1. USDC Balance
    balance = api.get_usdc_balance()
    print(f"\n1. USDC DISPONIBILE: ${balance:.2f}")

    # 2. Ordini aperti
    print("\n2. ORDINI APERTI:")
    try:
        orders = api.clob.get_orders(OpenOrderParams())
        if orders:
            for o in orders:
                print(f"   - {o}")
        else:
            print("   Nessun ordine aperto")
    except Exception as e:
        print(f"   Errore: {e}")

    # 3. Trade recenti (ultimi 50)
    print("\n3. ULTIMI TRADE ESEGUITI SUL CLOB:")
    try:
        trades = api.clob.get_trades(TradeParams())
        if trades:
            for i, t in enumerate(trades[:30]):
                if hasattr(t, '__dict__'):
                    d = t.__dict__
                    side = d.get('side', '?')
                    price = d.get('price', '?')
                    size = d.get('size', '?')
                    status = d.get('status', '?')
                    ts = d.get('created_at', d.get('timestamp', '?'))
                    asset_id = str(d.get('asset_id', d.get('token_id', '?')))[:20]
                    print(f"   [{i+1}] {side} | price={price} size={size} | status={status} | {ts} | {asset_id}...")
                elif isinstance(t, dict):
                    side = t.get('side', '?')
                    price = t.get('price', '?')
                    size = t.get('size', '?')
                    status = t.get('status', '?')
                    ts = t.get('created_at', t.get('timestamp', '?'))
                    asset_id = str(t.get('asset_id', t.get('token_id', '?')))[:20]
                    print(f"   [{i+1}] {side} | price={price} size={size} | status={status} | {ts} | {asset_id}...")
                else:
                    print(f"   [{i+1}] {t}")
            print(f"   ... totale: {len(trades)} trade")
        else:
            print("   Nessun trade trovato")
    except Exception as e:
        print(f"   Errore: {e}")

    # 4. Conta quanti SELL vs BUY
    print("\n4. RIEPILOGO TRADE:")
    try:
        if trades:
            buys = [t for t in trades if (getattr(t, 'side', None) or (t.get('side') if isinstance(t, dict) else '')) == 'BUY']
            sells = [t for t in trades if (getattr(t, 'side', None) or (t.get('side') if isinstance(t, dict) else '')) == 'SELL']
            print(f"   BUY:  {len(buys)}")
            print(f"   SELL: {len(sells)}")
    except Exception as e:
        print(f"   Errore: {e}")

    # 5. Verifica token positions dal JSON vs wallet
    print("\n5. POSIZIONI IN open_positions.json:")
    with open("logs/open_positions.json") as f:
        positions = json.load(f)
    print(f"   Totale: {len(positions)}")

    # Check a few token balances by trying small sells
    print("\n6. VERIFICA TOKEN NEL WALLET (primi 5):")
    for pos in positions[:5]:
        tid = pos["token_id"]
        strat = pos["strategy"]
        size = pos["size"]
        price = pos["price"]
        shares_expected = size / price if price > 0 else 0
        print(f"   {strat} | ${size} @ {price:.4f} | shares attese: {shares_expected:.1f}")
        try:
            book = api.get_order_book(tid)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 0
            print(f"     Book: bid={best_bid:.4f} ask={best_ask:.4f}")
        except Exception as e:
            print(f"     Book error: {e}")

    print("\n" + "=" * 70)

if __name__ == "__main__":
    main()
