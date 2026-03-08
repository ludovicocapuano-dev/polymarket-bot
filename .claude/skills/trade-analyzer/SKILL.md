---
name: trade-analyzer
description: Analisi post-trade approfondita con domain intelligence. Analizza trade specifici o la giornata, confronta con pattern storici, produce raccomandazioni parametriche. Use when user says "analizza trade", "perche' ho perso", "analisi trade", "cosa e' andato storto", "review trade".
---

# Trade Analyzer — Post-Trade Intelligence

Analisi approfondita di trade specifici con confronto pattern storici e raccomandazioni parametriche.

## Contesto
Consulta prima di ogni analisi:
- `/root/.claude/projects/-root/memory/trade_insights.md` — pattern storici per citta', orizzonte, strategia
- `/root/.claude/projects/-root/memory/mistakes.md` — errori noti da non ripetere

## Procedura

### Step 1: Identifica trade da analizzare
Se l'utente specifica un trade, analizza quello. Altrimenti prendi gli ultimi 10 trade chiusi:
```bash
LOG=$(ls -t /root/polymarket_toolkit/logs/bot_*.log | head -1)
grep -E "WIN|LOSS|Trade chiuso" "$LOG" | tail -10
```

### Step 2: Per ogni trade, estrai
- Strategia, citta'/mercato, prezzo entry, edge stimato, confidence
- Numero fonti, orizzonte (same-day/+1d/+2d)
- Outcome (WIN/LOSS), PnL
- Skip reasons dei trade simili che sono stati filtrati

### Step 3: Pattern matching
Confronta con pattern storici:
- Questa citta' ha WR storico buono o cattivo?
- Questo orizzonte e' profittevole per questa strategia?
- Trade simili (stessa fascia di prezzo, stesso tipo) hanno WR positivo?

Consulta `references/known-patterns.md` per pattern documentati.

### Step 4: Root cause analysis (per le LOSS)

#### Checklist diagnostica:
1. **Edge insufficiente?** — edge < min_edge per quel orizzonte?
2. **Forecast sbagliato?** — WU divergeva da OpenMeteo? Uncertainty alta?
3. **Prezzo troppo alto?** — payoff ratio < 0.30?
4. **Single source?** — una sola fonte confermava il trade?
5. **Timing sbagliato?** — trade piazzato troppo presto/tardi rispetto al settlement?
6. **Mercato illiquido?** — spread alto, depth basso?

### Step 5: Raccomandazione parametrica
Per ogni pattern di perdita identificato, proponi una modifica SPECIFICA:
- Quale parametro cambiare (con file e riga)
- Da quale valore a quale valore
- Impatto stimato: quanti trade bloccherebbe, quante loss evitate

### Step 6: Simulazione impatto
```bash
python3 /root/polymarket_toolkit/backtest_replay.py --compare
```
Verifica che la modifica proposta non blocchi troppi trade vincenti.

### Step 7: Aggiorna memoria
Se trovato pattern nuovo e significativo:
- Aggiorna `trade_insights.md` con il nuovo pattern
- Se errore sistemico: aggiorna `mistakes.md`

## Output
Report strutturato con:
1. Tabella trade analizzati
2. Root cause per ogni loss
3. Raccomandazione parametrica con simulazione
4. Aggiornamento memoria (se applicabile)

## Common Issues

### Trade con edge "alto" ma loss
Spesso l'edge stimato e' inflazionato da una singola fonte divergente. Verificare sempre il numero di fonti concordanti.

### Loss ripetute sulla stessa citta'
Alcune citta' hanno sistematicamente forecast meno accurati (es. Toronto WU vs OpenMeteo divergono di 12°C). Verificare il pattern citta'-specifico.

### Loss su next-day con prezzo alto
BUY_NO a >$0.75 su +1d ha payoff 0.33:1 — serve WR >75% per break-even. Se il WR storico per quel orizzonte e' <75%, il parametro min_payoff e' troppo basso.
