---
description: Verifica stato del risk manager - esposizione, correlazioni, stop-loss, circuit breaker
allowed-tools: [Read, Grep, Bash]
---

# Risk Check

Verifica completa dello stato del risk manager.

## Procedura

### 1. Esposizione
- Capitale totale, esposto, disponibile, floor
- % esposta per strategia
- Numero posizioni aperte vs max consentite

### 2. Stop-loss e cooldown
- Cerca `stop_loss`, `cooldown`, `BLOCKED` nei log recenti
- Mercati in cooldown attivo
- Consecutive losses per strategia

### 3. Circuit breaker feed
- Cerca `circuit`, `rate limit`, `429`, `error` nei log recenti
- Feed in errore o throttled (GDELT, Finlight, Glint, Twitter)

### 4. Drift e calibrazione
- Cerca `DRIFT`, `CALIBRATION` nei log
- Win rate attuale vs storico per strategia

### 5. Correlazione
- Cerca `CORRELATION`, `tema`, `cluster` nei log
- Esposizione per tema vs max 40%

### Output
Report strutturato con semafori: OK / WARNING / ALERT per ogni area.
