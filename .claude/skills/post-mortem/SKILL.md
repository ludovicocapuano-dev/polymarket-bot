---
name: post-mortem
description: Analisi post-mortem della giornata di trading. PnL, WR per strategia e citta', skip reasons, filtri, raccomandazioni. Aggiorna memoria persistente. Use when user says "post-mortem", "analisi giornata", "com'e' andata oggi", "recap".
---

# Post-Mortem Giornaliero

Analisi completa della giornata con aggiornamento memoria.

## Procedura

### Step 1: Identifica log del giorno
```bash
ls -t /root/polymarket_toolkit/logs/bot_*.log | head -3
```

### Step 2: Trade chiusi oggi
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
grep -E "WIN|LOSS|REDEEM|pnl=|close_trade" "$LOG"
```

### Step 3: Breakdown per strategia
Per weather, resolution_sniper, negrisk_arb, holding_rewards, favorite_longshot:
- Trade aperti/chiusi
- WR (win/total)
- PnL lordo e netto

### Step 4: Weather deep-dive
- WR per citta': `grep "WEATHER.*BUY" "$LOG" | grep -oP '(?<=city=)\w+'`
- WR per orizzonte (same-day vs +1d vs +2d)
- WR per tipo (BUY_NO vs BUY_YES)
- Edge medio entry vs edge realizzato
- Skip reasons: `grep "WEATHER-SKIP" "$LOG" | grep -oP '(?<=WEATHER-SKIP\] )\w+' | sort | uniq -c | sort -rn`

### Step 5: Filtri — troppo stretti o troppo larghi?
- 0 opportunita' per >2h consecutive = troppo stretto
- >3 loss consecutive sulla stessa citta'/orizzonte = troppo largo
- Confronta con dati storici da `references/trade-patterns.md`

### Step 6: Posizioni aperte — rischio overnight
```bash
python3 -c "
import json
pos = json.load(open('/root/polymarket_toolkit/logs/open_positions.json'))
for p in sorted(pos, key=lambda x: x.get('size',0), reverse=True)[:10]:
    print(f\"{p.get('strategy','?'):20s} {p.get('question','?')[:50]:50s} size=\${p.get('size',0):.2f}\")
print(f'Totale: {len(pos)} posizioni, \${sum(p.get(\"size\",0) for p in pos):.2f} deployato')
"
```

### Step 7: Metriche
- Profit Factor = gross_wins / gross_losses
- Capital efficiency = PnL / capitale medio deployato

### Step 8: Aggiorna memoria
Se nuovi pattern significativi:
- Aggiorna `/root/.claude/projects/-root/memory/trade_insights.md`
- Se errori nuovi: aggiorna `/root/.claude/projects/-root/memory/mistakes.md`

### Step 9: Raccomandazioni
3-5 azioni concrete per domani con dati a supporto.

## Output
Report completo in italiano con tabelle e raccomandazioni actionable.
