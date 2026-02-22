"""
Diagnostica vendite — analizza i trade SELL recenti per capire il recupero reale.
"""
import json
import sys
import logging
from datetime import datetime

logging.basicConfig(level=logging.WARNING)

from config import Config
from utils.polymarket_api import PolymarketAPI
from py_clob_client.clob_types import TradeParams

def main():
    config = Config.from_env()
    api = PolymarketAPI(config.creds)
    api.authenticate()

    if not api._authenticated:
        print("ERRORE: Autenticazione fallita!")
        sys.exit(1)

    # Get ALL trades
    print("Recupero trade dal CLOB...")
    trades = api.clob.get_trades(TradeParams())
    print(f"Totale trade: {len(trades)}")

    # Separate sells
    sells = []
    buys = []
    for t in trades:
        d = t.__dict__ if hasattr(t, '__dict__') else t
        side = d.get('side', '?')
        price = float(d.get('price', 0))
        size = float(d.get('size', 0))
        asset_id = str(d.get('asset_id', d.get('token_id', '')))
        status = d.get('status', '?')
        ts = d.get('created_at', d.get('timestamp', d.get('match_time', '')))

        entry = {
            'side': side,
            'price': price,
            'size': size,
            'asset_id': asset_id,
            'status': status,
            'ts': str(ts),
            'usdc': price * size,
        }

        if side == 'SELL':
            sells.append(entry)
        else:
            buys.append(entry)

    print(f"\nBUY: {len(buys)} | SELL: {len(sells)}")

    # Analyze sells
    print(f"\n{'='*80}")
    print(f"ANALISI SELL (ordine cronologico inverso)")
    print(f"{'='*80}")

    total_sell_usdc = 0
    for i, s in enumerate(sells[:50]):
        usdc = s['usdc']
        total_sell_usdc += usdc
        tid_short = s['asset_id'][:16]
        print(f"  [{i+1:2d}] SELL {s['size']:>10.2f} shares @ ${s['price']:.4f} = ${usdc:>8.2f} USDC | {s['status']} | {tid_short}... | {s['ts']}")

    print(f"\n  TOTALE SELL (primi {min(50, len(sells))}): ${total_sell_usdc:.2f}")

    # Total buy/sell
    total_buy_usdc = sum(b['price'] * b['size'] for b in buys)
    total_all_sell_usdc = sum(s['usdc'] for s in sells)
    print(f"\n{'='*80}")
    print(f"RIEPILOGO COMPLETO")
    print(f"{'='*80}")
    print(f"  Totale BUY:  ${total_buy_usdc:.2f} ({len(buys)} trade)")
    print(f"  Totale SELL: ${total_all_sell_usdc:.2f} ({len(sells)} trade)")
    print(f"  Net flow:    ${total_all_sell_usdc - total_buy_usdc:+.2f}")
    print(f"  USDC attuale: ${api.get_usdc_balance():.2f}")

    # Match sell_losers positions with CLOB sells
    print(f"\n{'='*80}")
    print(f"MATCH POSIZIONI VENDUTE vs CLOB")
    print(f"{'='*80}")

    # Load positions that were supposed to be sold (the ones removed from open_positions)
    # We know the sell criteria: crypto_5min BUY_YES < $0.05, data_driven BUY_YES < $0.15
    # Let's check which tokens from sells match our known positions
    with open("logs/open_positions.json") as f:
        current_pos = json.load(f)
    current_tokens = {p['token_id'] for p in current_pos}

    # Find sells of tokens NOT in current positions (i.e., sold and removed)
    sold_tokens = {}
    for s in sells:
        tid = s['asset_id']
        if tid not in sold_tokens:
            sold_tokens[tid] = []
        sold_tokens[tid].append(s)

    print(f"  Token unici venduti: {len(sold_tokens)}")
    print(f"  Token ancora in posizione: {len(current_tokens)}")

    # Show tokens sold that are NOT in current positions
    removed_sells = {tid: trades for tid, trades in sold_tokens.items() if tid not in current_tokens}
    print(f"  Token venduti e rimossi: {len(removed_sells)}")

    total_recovered = 0
    for tid, trades_list in removed_sells.items():
        for t in trades_list:
            total_recovered += t['usdc']
            print(f"    {tid[:20]}... SELL {t['size']:.2f}@{t['price']:.4f} = ${t['usdc']:.2f}")

    print(f"\n  TOTALE RECUPERATO (token rimossi): ${total_recovered:.2f}")


if __name__ == "__main__":
    main()
