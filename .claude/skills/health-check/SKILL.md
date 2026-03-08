---
name: health-check
description: Health check rapido del bot di trading. Verifica processo, errori, log, scanning, posizioni. Use when user says "health check", "stato bot", "il bot funziona?", "check bot".
---

# Health Check Bot

Controllo rapido dello stato del bot Polymarket.

## Procedura

### Step 1: Processo bot
```bash
ps aux | grep bot.py | grep -v grep
```
- Se non running: **ALERT**
- Se multipli processi: **ALERT** (duplicati)

### Step 2: Log piu' recente
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log 2>/dev/null | head -1)
echo "Log: $LOG"
echo "Ultima riga: $(tail -1 "$LOG")"
echo "Eta': $(stat -c %Y "$LOG" | xargs -I{} bash -c 'echo $(( ($(date +%s) - {}) / 60 )) min fa')"
```
- Log > 10 min: **WARNING**

### Step 3: Errori recenti
```bash
grep -c "ERROR" "$LOG" | xargs -I{} echo "Errori totali: {}"
grep "ERROR" "$LOG" | tail -5
```

### Step 4: Scanning attivo
```bash
grep "WEATHER.*Scan\|WEATHER.*scan\|weather.*opportunit" "$LOG" | tail -3
grep "SNIPER\|NEGRISK\|FAV-LONG\|HOLDING" "$LOG" | tail -5
```

### Step 5: Posizioni aperte
```bash
python3 -c "import json; pos=json.load(open('/root/polymarket_toolkit/logs/open_positions.json')); print(f'Posizioni: {len(pos)}'); total=sum(p.get('size',0) for p in pos); print(f'Capitale deployato: \${total:.2f}')"
```

### Step 6: Output
Report con semafori:
- **OK**: bot running, log fresco, scanning attivo
- **WARNING**: log vecchio, 0 scan recenti, errori sporadici
- **ALERT**: bot non running, errori critici, nessun log
