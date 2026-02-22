---
description: Regole per modifiche al risk manager, sizing, stop-loss e position management
globs: ["utils/risk_manager.py", "bot.py"]
---

# Regole Risk Management

## Kelly Sizing
- Kelly DEVE essere proporzionale per strategia (rimosso floor fisso $50 in v7.0)
- max_bet_size = $40 (Becker sweet spot: $100-$1K)
- Ogni strategia ha il suo budget allocato via set_strategy_budget()

## Stop-Loss Loop (BUG CRITICO risolto v8.0)
- Bid sanity check: se bid < 50% dell'entry price, HOLD (order book vuoto)
- Stop-loss cooldown: dopo stop loss, mercato bloccato per 4h (register_stop_loss)
- MAI rimuovere questi check senza capire il bug originale ($12.50 persi ogni 30min)

## Anti-Hedging
- Blocco posizioni opposte sullo stesso mercato (v7.0)
- Exposure limit: max 15% capitale per singolo mercato

## Position Manager
- Triple-barrier: STOP_LOSS > TIME_EXIT > TAKE_PROFIT
- Emergency capital recovery se USDC < $10 e > 5 posizioni aperte
- Purge posizioni stale dopo 48h
