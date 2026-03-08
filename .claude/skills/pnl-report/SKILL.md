---
name: pnl-report
description: Report PnL dettagliato con breakdown per strategia, categoria, periodo. Use when user says "pnl", "profitti", "perdite", "quanto ho guadagnato", "report pnl", "performance".
---

# PnL Report

Report dettagliato Profit & Loss del bot.

## Argomenti
L'utente puo' specificare: --oggi, --settimana, --tutto. Default: oggi.

## Procedura

### Step 1: Raccogli trade
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
grep -E "\[PNL\]|\[PAPER\]|\[POSITION-MGR\]" "$LOG"
```
Parsa anche `logs/trades.json` se disponibile.

### Step 2: Breakdown per strategia
```
| Strategia      | Trades | Vinti | Persi | PnL      | WR%  | Avg PnL/trade |
|----------------|--------|-------|-------|----------|------|---------------|
```

### Step 3: Timeline
- PnL cumulativo nel tempo
- Ore di picco per trade profittevoli

### Step 4: Metriche chiave
- PnL totale netto
- ROI su capitale iniziale
- Max drawdown
- Miglior/peggior trade singolo
- Capital efficiency (PnL / capitale allocato)

### Step 5: Raccomandazioni
- Segnala strategie in perdita
- Suggerisci ribilanciamento allocazione se necessario

## Output
Report dettagliato con tabelle in italiano.
