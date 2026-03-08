---
name: scan
description: Scan manuale opportunita' su tutte le strategie attive - weather, negrisk arb, favorite-longshot, holding rewards, sniper. Use when user says "scan", "opportunita'", "cosa c'e'", "mercati".
---

# Scan Opportunita'

Mostra le opportunita' trovate dall'ultimo ciclo del bot.

## Procedura

### Step 1: Ultimo ciclo
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
grep "Ciclo #" "$LOG" | tail -1
```

### Step 2: Per ogni strategia

#### Weather
```bash
grep -E "WEATHER.*Scan|WEATHER-SKIP" "$LOG" | tail -10
```
Mercati scansionati, opportunita', skip reasons.

#### NegRisk Arb
```bash
grep "NEGRISK-ARB" "$LOG" | tail -5
```
Eventi scansionati, arb trovati, tipo e profit%.

#### Favorite-Longshot
```bash
grep "FAV-LONG" "$LOG" | tail -5
```
Mercati eligible, top opportunita' con edge.

#### Holding Rewards
```bash
grep "HOLD-REWARDS" "$LOG" | tail -5
```
Mercati eligible, nuovi vs gia' in portafoglio.

#### Resolution Sniper
```bash
grep "SNIPER" "$LOG" | tail -5
```

### Step 3: Riepilogo
Tabella con tutte le strategie e opportunita' trovate. Evidenzia se qualche strategia non ha scansionato.
