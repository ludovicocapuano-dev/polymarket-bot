---
name: deploy-safe
description: Deploy sicuro del bot con validazione pre-avvio, kill duplicati, verifica import, controllo parametri, monitoraggio post-avvio. Use when user says "riavvia bot", "restart", "deploy", "riavvia", "avvia bot".
---

# Deploy Safe — Bot Deployment Sicuro

Sequential workflow con validazione a ogni step per riavviare il bot senza rischi.

## CRITICAL: Seguire TUTTI gli step in ordine. Non saltare validazioni.

## Procedura

### Step 1: Kill istanze esistenti
```bash
ps aux | grep bot.py | grep -v grep
```
Se ci sono processi:
```bash
# Identifica PID e kill con grazia prima, poi force
kill <PID>
sleep 2
ps aux | grep bot.py | grep -v grep
# Se ancora vivo:
kill -9 <PID>
```
**Validazione**: `ps aux | grep bot.py | grep -v grep` deve restituire vuoto.

### Step 2: Verifica import moduli
```bash
cd /root/polymarket_toolkit && python3 -c "
import bot
import weather
import config
from monitoring import quant_metrics, hrp, kyle_lambda
from utils import risk_manager, kalman_forecast
from execution import execution_agent
from strategies import abandoned_position, cross_platform_arb
print('OK: tutti i moduli importati')
"
```
**Se errore**: NON procedere. Mostra l'errore e chiedi all'utente.

### Step 3: Verifica coerenza parametri
```bash
python3 -c "
from config import Config
c = Config()
# Allocazioni sommano a 100%
alloc = c.weather_pct + c.sniper_pct + c.bond_pct + c.event_pct + c.whale_pct
print(f'Allocazione totale: {alloc*100:.0f}% (deve essere 100%)')
assert abs(alloc - 1.0) < 0.01, f'ERRORE: allocazione = {alloc}'
# Capitale e limiti
print(f'Capital: \${c.total_capital}')
print(f'Max bet: \${c.max_bet_size}')
print(f'Max daily loss: \${c.max_daily_loss}')
print(f'Reserve floor: \${c.total_capital * 0.20:.0f}')
print('OK: parametri coerenti')
"
```
**Se errore**: NON procedere. Correggi il parametro.

### Step 4: Verifica spazio disco e log
```bash
df -h /root | tail -1
ls -t /root/polymarket_toolkit/logs/bot_*.log | head -3
wc -l /root/polymarket_toolkit/logs/open_positions.json
```

### Step 5: Avvia bot
```bash
echo 'CONFERMO' | python3 bot.py --live &
sleep 5
ps aux | grep bot.py | grep -v grep
```
**Validazione**: esattamente 1 processo bot.py. Se 0 o >1: STOP.

### Step 6: Monitoraggio primi 30 secondi
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
sleep 25
grep -c ERROR "$LOG"
tail -20 "$LOG"
```
**Se ERROR nei primi 30s**: kill e rollback. Mostra l'errore.

### Step 7: Conferma
- PID del bot
- Log file in uso
- Errori: 0
- Stato: OK

## Rollback
Se qualsiasi step fallisce:
1. Kill il bot se avviato
2. Mostra l'errore esatto
3. NON tentare di fixare automaticamente — chiedi all'utente

## Common Issues

### Import error dopo modifica
Un file modificato ha un errore di sintassi. Runnare `python3 -c "import <modulo>"` per identificare.

### Duplicati dopo restart
Il `&` in bash puo' creare duplicati se eseguito piu' volte. SEMPRE verificare con `ps aux` prima e dopo.

### Bot muore subito dopo avvio
Spesso: credenziali scadute, API key mancante, .env corrotto. Controllare le prime 10 righe del log.
