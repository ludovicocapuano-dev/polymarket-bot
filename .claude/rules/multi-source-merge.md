---
description: Regole per merge dati da fonti multiple (Finlight, GDELT, Binance, ecc.)
globs: ["strategies/event_driven.py", "utils/gdelt_feed.py", "utils/finlight_feed.py"]
---

# Regole Merge Multi-Fonte

## Principio fondamentale
- MAI sommare dati da fonti diverse (double counting)
- Il merge sceglie sempre UNA fonte come base per decisione

## Tabella merge
| Situazione                  | Comportamento                                        |
|-----------------------------|------------------------------------------------------|
| Solo fonte A ha dati        | Usa fonte A                                          |
| Solo fonte B ha dati        | Usa fonte B (fallback)                               |
| Entrambe hanno dati         | Prendi fonte con segnale piu' forte                  |
| Entrambe concordano (>0.3)  | Boost 10% alla strength (conferma cross-fonte)       |
| Fonti in disaccordo         | Usa quella con piu' articoli; a parita' la piu' precisa |

## Aggiunta nuove fonti
Quando si aggiunge una nuova fonte dati:
1. Produrre gli stessi tipi (NewsArticle, NewsSentiment) per interoperabilita'
2. Implementare graceful degradation (circuit breaker dopo N errori)
3. Aggiornare i metodi _merge_* in event_driven.py
4. Documentare precision/recall relativa vs fonti esistenti
5. Applicare discount se meno precisa di Finlight
