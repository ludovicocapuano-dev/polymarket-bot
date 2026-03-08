# Trade Patterns per Post-Mortem

## WR target per strategia
| Strategia | WR target | WR break-even | Note |
|-----------|-----------|---------------|------|
| weather BUY_NO | > 80% | ~74% | Tail selling, payoff basso |
| weather BUY_YES | > 30% | ~15% | Basso prezzo, alto payoff |
| resolution_sniper | > 95% | ~90% | Quasi risk-free |
| negrisk_arb | > 90% | N/A | Arb meccanico |
| holding_rewards | N/A | N/A | 4% APY, non trade-based |
| favorite_longshot | > 55% | ~50% | Fee-free, bias exploitation |

## Frequenza attesa
- Weather: 2-5 trade/giorno
- Resolution sniper: 0-2 trade/giorno
- NegRisk arb: 0-1 trade/giorno
- Favorite-longshot: 0-3 trade/giorno
- Holding rewards: 0-1 trade/settimana

## Red flags
- 0 scan weather per > 2 ore = feed rotto o filtri troppo stretti
- > 3 loss consecutive stessa citta' = pattern citta'-specifico, consider blacklist
- WR giornaliero < 50% = qualcosa e' cambiato, investigare
- PnL giornaliero < -$100 = drawdown control dovrebbe aver ridotto sizing
