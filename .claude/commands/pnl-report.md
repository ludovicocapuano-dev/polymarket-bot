---
description: Genera report dettagliato PnL con breakdown per strategia, categoria, e periodo
argument-hint: [--oggi | --settimana | --tutto]
allowed-tools: [Read, Glob, Grep, Bash]
---

# PnL Report

Genera un report dettagliato del Profit & Loss del bot.

## Argomenti
L'utente ha invocato: $ARGUMENTS

Default: report di oggi.

## Procedura

### 1. Raccogli trade
- Cerca file trade salvati dal risk manager in `/root/polymarket_toolkit/`
- Parsa i log da `logs/bot_*.log` per trade paper e live
- Pattern da cercare: `[PAPER]`, `[PNL]`, `[POSITION-MGR]`

### 2. Breakdown per strategia
```
| Strategia      | Trades | Vinti | Persi | PnL      | WR%  | Avg PnL/trade |
|----------------|--------|-------|-------|----------|------|---------------|
```

### 3. Breakdown per categoria (event_driven)
```
| Categoria        | Trades | PnL    | WR%  | Becker Expected |
|------------------|--------|--------|------|-----------------|
| political        |        |        |      | +$18.6M         |
| geopolitical     |        |        |      |                 |
| macro            |        |        |      |                 |
| crypto_regulatory|        |        |      |                 |
| tech             |        |        |      |                 |
```

### 4. Timeline
- PnL cumulativo nel tempo (se dati sufficienti)
- Ore di picco per trade profittevoli

### 5. Metriche chiave
- PnL totale netto
- ROI su capitale iniziale
- Max drawdown
- Miglior/peggior trade singolo
- Capital efficiency (PnL / capitale allocato)

### 6. Raccomandazioni
- Segnala strategie in perdita
- Confronta performance reale vs Becker expected
- Suggerisci ribilanciamento allocazione se necessario
