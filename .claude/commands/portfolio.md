---
description: Report portfolio real-time con PnL, posizioni attive, cash, e stato strategie
allowed-tools: [Read, Grep, Bash]
---

# Portfolio Report Real-Time

Genera un report istantaneo del portfolio Polymarket.

## Procedura

### 1. Portfolio reale
- Cerca l'ultimo `[PORTFOLIO] REALE:` nel log piu' recente (`ls -t logs/bot_*.log | head -1`)
- Estrai: deposited, cash, positions, total, PnL, PnL%, n_attive, n_redeemable

### 2. Trade aperti oggi
- Cerca `APERTO` nel log di oggi
- Conta per strategia (weather, favorite_longshot, holding_rewards, resolution_sniper, negrisk_arb)
- Mostra importo totale investito oggi

### 3. Risoluzioni oggi
- Cerca `RISOLTO`, `WIN`, `LOSS`, `Trade chiuso` nel log di oggi
- Calcola W/L e PnL delle risoluzioni

### 4. Cash disponibile
- Cash attuale - reserve floor ($700) = disponibile per nuovi trade
- Segnala se sotto il 10% del portfolio

### 5. Stato strategie
- Ultimi scan di ogni strategia (weather, fav-long, hold-rewards, negrisk-arb)
- Numero opportunita' trovate per scan

### Output
Tabella riassuntiva compatta con tutti i dati. In italiano.
