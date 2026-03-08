#!/usr/bin/env python3
"""Refresh on-chain weather trade data from Polymarket Data API."""
import json, os, requests
from dotenv import load_dotenv
load_dotenv()

funder = os.getenv('FUNDER_ADDRESS', '')
if not funder:
    print("[REFRESH] No FUNDER_ADDRESS")
    exit(1)

url = f'https://data-api.polymarket.com/positions?user={funder}&limit=500&sortBy=CASHPNL&sortOrder=desc'
r = requests.get(url, timeout=15)
positions = r.json()

weather_kw = ['temperature', 'temp', 'degrees', 'high in', 'low in', 'above', 'below']
weather = []
for p in positions:
    title = (p.get('title', '') + ' ' + p.get('question', '')).lower()
    if any(kw in title for kw in weather_kw):
        pnl = p.get('cashPnl', 0)
        weather.append({
            'timestamp': '', 'strategy': 'weather',
            'city': '', 'direction': 'BUY_NO' if p.get('avgPrice', 0) > 0.5 else 'BUY_YES',
            'price': p.get('avgPrice', 0), 'size': p.get('initialValue', 0),
            'edge': 0, 'confidence': 0, 'sources': 1, 'horizon': 0,
            'outcome': 'WIN' if pnl > 0 else ('LOSS' if pnl < 0 else 'OPEN'),
            'pnl': pnl, 'question': p.get('title', ''), 'payoff': 0, 'uncertainty': 0,
        })

with open('logs/weather_trades_onchain.json', 'w') as f:
    json.dump(weather, f, indent=2)

wins = [t for t in weather if t['outcome'] == 'WIN']
losses = [t for t in weather if t['outcome'] == 'LOSS']
print(f"[REFRESH] {len(weather)} weather trades: {len(wins)}W/{len(losses)}L PnL=${sum(t['pnl'] for t in weather):.2f}")
