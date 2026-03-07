---
description: Scan manuale delle opportunita' su tutte le strategie attive
allowed-tools: [Read, Grep, Bash]
---

# Scan Opportunita' Manuale

Mostra le opportunita' trovate dall'ultimo ciclo del bot su tutte le strategie.

## Procedura

### 1. Ultimo ciclo
- Trova il log piu' recente: `ls -t logs/bot_*.log | head -1`
- Cerca l'ultimo blocco di scan (dalla riga `Ciclo #` piu' recente in poi)

### 2. Per ogni strategia, mostra:

#### Weather
- Pattern: `[WEATHER] Scan` — numero mercati, opportunita'
- Pattern: `[WEATHER-SKIP]` — motivi di skip (low edge, low EV, etc.)
- Migliore opportunita' con edge, EV, payoff

#### NegRisk Arb
- Pattern: `[NEGRISK-ARB]` — eventi scansionati, arb trovati
- Se trovati: tipo (buy_all/sell_all), sum, profit%

#### Favorite-Longshot
- Pattern: `[FAV-LONG]` — mercati eligible, opportunita'
- Top opportunita' con edge, volume, categoria

#### Holding Rewards
- Pattern: `[HOLD-REWARDS]` — mercati eligible, gia' in portafoglio, nuovi

#### Resolution Sniper
- Pattern: `[SNIPER]` — segnali trovati

### 3. Riepilogo
Tabella con tutte le strategie e le opportunita' trovate nell'ultimo ciclo.
Evidenzia se qualche strategia non ha scansionato (errore o cooldown).
