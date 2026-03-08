---
name: parameter-tuner
description: Ottimizzazione guidata dei parametri del bot con simulazione what-if e backtest. Propone modifiche, simula impatto, chiede conferma. Use when user says "ottimizza", "tuning", "parametri", "migliora WR", "migliora profit", "cambia filtri".
---

# Parameter Tuner — Guided Optimization

Ottimizzazione parametri con ciclo proposta-simulazione-conferma.

## Procedura

### Step 1: Identifica obiettivo
Chiedi all'utente (se non specificato): quale metrica migliorare?
- **WR** (Win Rate) — filtrare trade marginali
- **Profit Factor** — migliorare rapporto win/loss
- **Sharpe** — ridurre varianza
- **Frequenza** — piu' trade mantenendo WR

### Step 2: Analizza stato corrente
```bash
python3 /root/polymarket_toolkit/scripts/run_quant_metrics.py
```
Raccogli PSR, DSR, WR, profit factor per ogni strategia.

### Step 3: Proponi 2-3 modifiche
Per ogni modifica proposta:
1. **Parametro**: nome, file, riga attuale
2. **Valore attuale** → **Valore proposto**
3. **Giustificazione**: basata su dati (log, backtest, Becker)
4. **Trade impattati**: quanti bloccati, quante loss evitate, quanti win persi

### Step 4: Simula impatto
```bash
python3 /root/polymarket_toolkit/backtest_replay.py --compare
```
Confronta filtri vecchi vs nuovi su trade storici.

### Step 5: Mostra risultati
```
| Parametro        | Prima  | Dopo   | Trade bloccati | Loss evitate | Win perse |
|------------------|--------|--------|----------------|--------------|-----------|
| min_edge +1d     | 0.05   | 0.08   | 8              | 6            | 2         |
| min_confidence   | 0.45   | 0.55   | 5              | 4            | 1         |
```

### Step 6: Chiedi conferma
Presenta il trade-off e chiedi conferma prima di applicare.

### Step 7: Applica (se confermato)
- Modifica il file con Edit tool
- Riavvia il bot (usa skill `deploy-safe` se disponibile)
- Monitora primi 30s di log

## Parametri comuni da ottimizzare

### Weather (`weather.py`)
- `min_edge`: per orizzonte (same-day, +1d, +2d)
- `min_confidence`: soglia minima
- `min_payoff`: payoff ratio minimo
- `max_price_no` / `max_price_yes`: prezzo max entry

### Risk Manager (`utils/risk_manager.py`)
- `kelly_fraction`: aggressivita' Kelly
- `max_daily_loss`: limite perdita giornaliera
- `max_consecutive_losses`: halt dopo N loss

### Config (`config.py`)
- `total_capital`: base per sizing
- `max_bet`: limite singolo trade
- Budget allocazione per strategia

## Common Issues

### Modifica troppo aggressiva
Se la simulazione mostra >30% dei trade vincenti bloccati, la modifica e' troppo aggressiva. Proponi un valore intermedio.

### Overfitting
Se la modifica e' basata su <20 trade, segnala rischio overfitting. Suggerisci di aspettare piu' dati.
