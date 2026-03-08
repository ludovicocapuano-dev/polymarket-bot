---
name: risk-check
description: Verifica stato risk manager - esposizione, correlazioni, stop-loss, circuit breaker, drawdown. Use when user says "risk", "rischio", "esposizione", "risk check", "stop loss".
---

# Risk Check

Verifica completa dello stato del risk manager.

## Procedura

### Step 1: Esposizione
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
grep -E "PORTFOLIO|capital|esposto|floor" "$LOG" | tail -5
```
Capitale totale, esposto, disponibile, floor. % esposta per strategia. Posizioni aperte vs max.

### Step 2: Stop-loss e cooldown
```bash
grep -E "stop_loss|cooldown|BLOCKED|HALT|CONSEC_LOSS" "$LOG" | tail -10
```
Mercati in cooldown. Consecutive losses per strategia.

### Step 3: Circuit breaker feed
```bash
grep -E "circuit|rate.limit|429|error|DISABLED" "$LOG" | tail -10
```
Feed in errore o throttled.

### Step 4: Drift e calibrazione
```bash
grep -E "DRIFT|CALIBRATION|CUSUM|HEALTH" "$LOG" | tail -5
```
Win rate attuale vs storico per strategia.

### Step 5: Drawdown
```bash
grep -E "DRAWDOWN|DEGRADATION|daily_pnl" "$LOG" | tail -5
```
Livello drawdown giornaliero e moltiplicatore attivo.

### Step 6: Correlazione
```bash
grep -E "CORRELATION|tema|cluster" "$LOG" | tail -5
```
Esposizione per tema vs max 40%.

## Output
Report con semafori: **OK** / **WARNING** / **ALERT** per ogni area.
