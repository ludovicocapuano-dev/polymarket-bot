---
name: strategy-status
description: Dashboard stato attuale di tutte le strategie, feed, posizioni aperte del bot. Use when user says "stato strategie", "strategy status", "dashboard", "cosa sta facendo il bot".
---

# Strategy Status

Dashboard compatta del bot e strategie.

## Procedura

### Step 1: Stato bot
```bash
ps aux | grep "python.*bot.py" | grep -v grep
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
grep "Ciclo #" "$LOG" | tail -1
```

### Step 2: Stato strategie
Per ciascuna cerca nel log l'ultimo scan e trade:
```bash
for pat in WEATHER SNIPER NEGRISK FAV-LONG HOLD-REWARDS ABANDONED CROSS-PLATFORM; do
    echo "=== $pat ==="; grep "$pat" "$LOG" | tail -3
done
```

### Step 3: Stato feed
```bash
grep -E "FINLIGHT|GDELT|BINANCE|GLINT|TWITTER|WEATHER.*Provider" "$LOG" | tail -10
```

### Step 4: Posizioni aperte
```bash
python3 -c "
import json
pos = json.load(open('/root/polymarket_toolkit/logs/open_positions.json'))
by_strat = {}
for p in pos:
    s = p.get('strategy', '?')
    by_strat[s] = by_strat.get(s, 0) + 1
for s, n in sorted(by_strat.items()):
    print(f'  {s}: {n}')
print(f'Totale: {len(pos)}')
"
```

## Output
```
Bot: RUNNING (live) | Ciclo #XXX
Strategie: X attive
Feed: Weather OK | GDELT OK | Binance OK
Posizioni: X aperte per strategia
```
