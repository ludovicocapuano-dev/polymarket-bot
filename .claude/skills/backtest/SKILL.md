---
name: backtest
description: Backtest e analisi performance storica delle strategie. Usa Becker Dataset e log bot. Use when user says "backtest", "performance storica", "simulazione", "come andava".
---

# Backtest Strategy

Analisi performance storica di una o piu' strategie.

## Argomenti
Se l'utente specifica una strategia, analizza solo quella. Altrimenti tutte.

## Procedura

### Step 1: Raccogli dati
- Trade da `logs/trades.json`
- Log recenti da `logs/bot_*.log`
- Se disponibile: Becker Dataset (`/root/becker-dataset/data/`)

### Step 2: Metriche per strategia
Per ogni strategia attiva:
- **PnL totale**
- **Win rate**: % trade vinti
- **Avg edge**: edge medio dei trade
- **Avg size**: dimensione media
- **Sharpe ratio**: PnL / stddev
- **Max drawdown**: peggior serie di perdite
- **Trades/giorno**: frequenza

### Step 3: Output
```
| Strategia      | PnL     | WR%  | Trades | Avg Edge | Avg Size |
|----------------|---------|------|--------|----------|----------|
```

### Step 4: Simulazione what-if
Se l'utente chiede, eseguire `backtest_replay.py`:
```bash
python3 /root/polymarket_toolkit/backtest_replay.py --compare
```

### Step 5: Raccomandazioni
- WR < 50%: segnala per review
- PnL negativo: proponi riduzione allocazione
- PnL forte: proponi aumento allocazione
