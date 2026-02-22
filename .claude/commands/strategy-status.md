---
description: Mostra lo stato attuale di tutte le strategie, feed, e posizioni aperte del bot
allowed-tools: [Read, Glob, Grep, Bash]
---

# Strategy Status

Mostra lo stato corrente del bot e di tutte le strategie.

## Procedura

### 1. Stato bot
- Verifica se il bot e' in esecuzione: `ps aux | grep "python.*bot.py"`
- Modalita': paper o live
- Ultimo log: `ls -t logs/bot_*.log | head -1`

### 2. Stato strategie
Leggi il log piu' recente e cerca per ogni strategia:
- `[BOND]`, `[GABAGOOL]`, `[EVENT]`, `[WEATHER]`, `[ARB]`, `[WHALE]`, `[DATA]`, `[SNIPER]`
- Ultimo scan: quando e quante opportunita' trovate
- Ultimo trade: quando e con che risultato
- Eventuali errori recenti

### 3. Stato feed
- `[FINLIGHT]`: API connessa? Ultime news?
- `[GDELT]`: API connessa o circuit breaker attivo?
- `[BINANCE]`: WebSocket connesso? Simboli pronti?

### 4. Posizioni aperte
- Cerca `[POSITION-MGR]` e `[PNL]` nel log
- Numero posizioni aperte
- PnL unrealized se disponibile

### 5. Output
Presenta come dashboard compatta:
```
Bot: RUNNING (paper) | Ciclo #XXX | Uptime: Xh
Strategie: 7 attive, 2 eliminate
Feed: Finlight OK | GDELT OK | Binance OK
Posizioni: X aperte | PnL unrealized: $+X.XX
Ultimo trade: [strategia] $X.XX @ X.XXXX (Xmin fa)
```
