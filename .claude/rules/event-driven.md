---
description: Regole per modifiche alla strategia event_driven e ai feed di news (Finlight, GDELT)
globs: ["strategies/event_driven.py", "utils/finlight_feed.py", "utils/gdelt_feed.py"]
---

# Regole Event-Driven Strategy

## Anti-doppio-conteggio (CRITICO)
- Il merge multi-fonte (Finlight + GDELT) sceglie sempre UNA fonte come base
- MAI sommare articoli da fonti diverse
- Per breaking news: prendi la fonte con segnale piu' forte (|sentiment| * n_articles)
- Per market sentiment: prendi la fonte con piu' articoli; a parita' Finlight vince
- Per news_strength: max(finlight, gdelt); boost 10% solo se entrambe > 0.3

## CATEGORY_CONFIG (v8.0 Becker Dataset)
- politics: min_edge=0.02, confidence_boost=+0.10 (Becker: +$18.6M PnL)
- crypto_regulatory: min_edge=0.05 (hard to beat, ben calibrato)
- geopolitical: min_edge=0.04, confidence_boost=+0.05
- macro: min_edge=0.03
- tech: min_edge=0.04
- Ogni modifica ai parametri DEVE essere giustificata con dati Becker

## Feed news
- Finlight: piu' preciso per finanza/crypto, richiede API key
- GDELT: gratuito, migliore copertura political/geopolitical, tone meno preciso
- GDELT confidence cappata a 0.85, strength discount 10%
- Entrambi i feed DEVONO degradare gracefully (circuit breaker)
- Mai leggere o esporre API keys da .env
