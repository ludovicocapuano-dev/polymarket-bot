---
description: Regole generali per tutte le strategie del bot
globs: ["strategies/*.py", "bot.py", "config.py"]
---

# Regole Generali Strategie

## Allocazione v9.1
- high_prob_bond=30%, event_driven=25%, weather=20%, whale_copy=15%, data_driven=10%
- arb_gabagool=0% (DISABILITATO v9.1: exploit incrementNonce), arbitrage=0% (DISABILITATO v9.1)
- crypto_5min=0% (ELIMINATO), market_making=0% (ELIMINATO)
- Somma DEVE essere 100%. Config valida o il bot non parte.

## Sicurezza arb
- arb_gabagool e arbitrage NON riabilitare senza verifica settlement on-chain atomica
- Exploit: incrementNonce() invalida settlement dopo match CLOB, bot resta con posizione naked
- Le gambe arb sono sequenziali (gap 3-5s), non atomiche

## Pattern obbligatori per ogni strategia
- Ogni strategia ha try/except isolato nel main loop (se una crasha, le altre continuano)
- scan() riceve shared_markets (fetchati UNA volta per ciclo, non N volte)
- execute() controlla can_trade() prima di piazzare ordini
- Paper trading simula con random + slippage

## Modifiche strategie
- Performance negativa documentata = strategia ELIMINATA (non "fixata" all'infinito)
- Nuovi parametri DEVONO avere giustificazione da dati (Becker Dataset o paper trading)
- Sports = HARD blacklist per bond (Becker: -$17.4M PnL sport)

## File .env
- MAI leggere, mostrare, o modificare il file .env
- Contiene chiavi private e API keys
