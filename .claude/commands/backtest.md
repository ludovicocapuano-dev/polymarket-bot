---
description: Analizza performance storica delle strategie usando Becker Dataset e log del bot
argument-hint: [strategia] [--periodo giorni]
allowed-tools: [Read, Glob, Grep, Bash]
---

# Backtest Strategy

Analizza la performance storica di una o piu' strategie del bot.

## Argomenti
L'utente ha invocato: $ARGUMENTS

Se nessun argomento, analizza TUTTE le strategie attive.

## Procedura

### 1. Raccogli dati
- Leggi i trade salvati dal risk manager: cerca file `trades_*.json` o simili in `/root/polymarket_toolkit/`
- Leggi i log recenti da `logs/bot_*.log`
- Se disponibile, incrocia con Becker Dataset (`/root/becker-dataset/data/`)

### 2. Calcola metriche per strategia
Per ogni strategia attiva (bond, gabagool, event_driven, weather, arb, whale_copy, data_driven):
- **PnL totale**: somma profitti e perdite
- **Win rate**: % trade vinti
- **Avg edge**: edge medio dei trade
- **Avg size**: dimensione media
- **Sharpe ratio**: se possibile (PnL / stddev)
- **Max drawdown**: peggior serie di perdite consecutive
- **Trades/giorno**: frequenza operativa

### 3. Analisi per categoria (event_driven)
- PnL per event_type (political, macro, crypto_regulatory, geopolitical, tech)
- Confronta con Becker Dataset expected values

### 4. Output
Presenta i risultati come tabella ordinata per PnL:
```
| Strategia      | PnL     | WR%  | Trades | Avg Edge | Avg Size |
|----------------|---------|------|--------|----------|----------|
| high_prob_bond | +$X.XX  | XX%  | N      | X.XX%    | $XX      |
| ...            |         |      |        |          |          |
```

### 5. Raccomandazioni
- Strategie con WR < 50%: segnala per review
- Strategie con PnL negativo: proponi riduzione allocazione
- Strategie con PnL forte: proponi aumento allocazione
- Parametri fuori range ottimale Becker: segnala
