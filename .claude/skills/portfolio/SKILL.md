---
name: portfolio
description: Report portfolio real-time con PnL, posizioni attive, cash, stato strategie. Use when user says "portfolio", "posizioni", "quanto ho", "stato portafoglio", "balance".
---

# Portfolio Report Real-Time

Report istantaneo del portfolio Polymarket.

## Procedura

### Step 1: Portfolio reale
Cerca l'ultimo `[PORTFOLIO] REALE:` nel log piu' recente.
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
grep "PORTFOLIO.*REALE" "$LOG" | tail -1
```
Estrai: deposited, cash, positions, total, PnL, PnL%, n_attive, n_redeemable.

### Step 2: Trade aperti oggi
```bash
grep "APERTO" "$LOG" | tail -10
```
Conta per strategia. Mostra importo totale investito oggi.

### Step 3: Risoluzioni oggi
```bash
grep -E "RISOLTO|WIN|LOSS|Trade chiuso" "$LOG" | tail -10
```
Calcola W/L e PnL delle risoluzioni.

### Step 4: Cash disponibile
- Cash attuale - reserve floor ($700) = disponibile per nuovi trade
- Segnala se sotto il 10% del portfolio

### Step 5: Stato strategie
Ultimi scan di ogni strategia con numero opportunita' trovate.

## Output
Tabella riassuntiva compatta in italiano.
