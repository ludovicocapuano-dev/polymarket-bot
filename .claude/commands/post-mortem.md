---
description: Analisi post-mortem della giornata - PnL, WR per strategia/citta', filtri, raccomandazioni. Aggiorna memoria persistente.
allowed-tools: [Read, Grep, Bash, Glob, Write, Edit]
---

# Post-Mortem Giornaliero

Analisi completa della giornata di trading con aggiornamento della memoria persistente.

## Procedura

### 1. Identifica log del giorno
```bash
ls -t /root/polymarket_toolkit/logs/bot_*.log | head -3
```
Usa il log piu' recente (o tutti quelli di oggi se multipli).

### 2. Trade chiusi oggi
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
grep -E "WIN|LOSS|REDEEM|pnl=|close_trade" "$LOG"
```

### 3. Breakdown per strategia
Per ogni strategia attiva (weather, resolution_sniper, negrisk_arb, holding_rewards, favorite_longshot):
- Conta trade aperti/chiusi
- WR (win/total)
- PnL lordo e netto

### 4. Weather deep-dive
- WR per citta': `grep "WEATHER.*BUY" "$LOG" | grep -oP '(?<=city=)\w+'`
- WR per orizzonte (same-day vs +1d vs +2d)
- WR per tipo (BUY_NO vs BUY_YES)
- Edge medio entry vs edge realizzato
- Skip reasons: `grep "WEATHER-SKIP" "$LOG" | grep -oP '(?<=WEATHER-SKIP\] )\w+' | sort | uniq -c | sort -rn`

### 5. Filtri — troppo stretti o troppo larghi?
- 0 opportunita' per >2h consecutive = troppo stretto
- >3 loss consecutive sulla stessa citta'/orizzonte = troppo largo
- Confronta con WR storici da `/root/.claude/projects/-root/memory/trade_insights.md`

### 6. Posizioni aperte — rischio overnight
```bash
python3 -c "
import json
pos = json.load(open('/root/polymarket_toolkit/logs/open_positions.json'))
for p in sorted(pos, key=lambda x: x.get('size',0), reverse=True)[:10]:
    print(f\"{p.get('strategy','?'):20s} {p.get('question','?')[:50]:50s} size=\${p.get('size',0):.2f}\")
print(f'Totale: {len(pos)} posizioni, \${sum(p.get(\"size\",0) for p in pos):.2f} deployato')
"
```

### 7. Metriche
- Profit Factor = gross_wins / gross_losses
- Capital efficiency = PnL / capitale medio deployato
- Max drawdown intraday (se tracciabile)

### 8. Aggiorna memoria
Se ci sono nuovi pattern significativi:
- Aggiorna `/root/.claude/projects/-root/memory/trade_insights.md` con WR per citta'/orizzonte
- Se errori nuovi: aggiorna `/root/.claude/projects/-root/memory/mistakes.md`

### 9. Raccomandazioni
3-5 azioni concrete per domani:
- Filtri da modificare (con dati a supporto)
- Citta' problematiche
- Strategie da ribilanciare
- Rischi specifici (meteo estremo, eventi politici)

## Output
Report completo in italiano con tabelle e raccomandazioni actionable.
