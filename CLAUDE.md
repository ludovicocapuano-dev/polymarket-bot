# Preferenze
- Rispondi sempre in italiano
- Nessuna convenzione particolare di codice

# Progetto: Polymarket Multi-Strategy Trading Bot (v10.1.0)
Repo: https://github.com/ludovicocapuano-dev/polymarket-bot (privato)
Bot automatico di trading su Polymarket con 5 strategie attive (4 eliminate per performance negativa o sicurezza).
v8.0 ottimizzato con analisi Becker Dataset (115M trade, 381K mercati risolti).
v8.1: integrazione GDELT feed per event_driven + configurazione .claude/ (rules, agents, commands).
v9.0: architettura agentica a 6 layer (Signal Validator, Monitoring, Storage, Orchestrator, Risk, Execution).
v9.1: arb disabilitate per exploit incrementNonce(), weather max_bet cappato a $15, bugfix vari.
v9.2.1: VPIN toxic flow detection, VAMP pricing (Stoikov), flash move protection, riallocazione post-Gemchange.
v9.2.2: fix feed P0/P1 (redeemer conditionId, GDELT rate limit, Finlight backoff, whale API migration, Binance WS).
v9.2.3: Data API redeemable fast-path (1 chiamata vs N), strict conditionId validation (no zfill).
v10.0: Empirical Kelly con Monte Carlo — position sizing data-driven (bootstrap 10K paths, CV_edge haircut).
v10.0.1: fix redeemer GS013 — CTF-only routing (bypass NRA bug) + firma v=1 (msg.sender == owner).
v10.1.0: 10 bug critici profitability + spread dinamico pmxt data-driven.

## Architettura v9.0 — 6 Layer

| Layer | Componente | Descrizione |
|-------|-----------|-------------|
| Layer 2 | Signal Validator + Devil's Advocate | 8 gate checks pre-esecuzione (v10.1: EV net-of-fees, REVIEW accepted, MIN_CONF 0.50), contraddittorio deterministico |
| Layer 5 | Attribution + Drift + Calibration + Empirical Kelly | Brier score, concept drift, suggerimenti parametri, MC position sizing (v10.0) |
| Layer 0 | PostgreSQL + Redis | Storage persistente + event bus (opzionale, graceful degradation) |
| Layer 1 | Orchestrator Agent | Prioritizzazione mercati (CRITICAL/HIGH/MEDIUM/LOW/SKIP) |
| Layer 3 | Correlation Monitor + Tail Risk | Max 40% per tema, VaR 95%, worst-case analysis |
| Layer 4 | Execution Engine | TWAP per trade >$30, LIMIT_MAKER per trade piccoli |

## Struttura
- `/root/polymarket_toolkit/` — codice principale del bot
  - `bot.py` — entry point, orchestratore multi-strategia, position manager, integrazione 6 layer v9.0
  - `config.py` — configurazione centralizzata (da .env) + db_dsn/redis_url v9.0
  - `.env` — credenziali e parametri (NON toccare/leggere)
  - `crypto_5min.py` — DISABILITATO v7.0 (fees > edge)
  - `weather.py` — strategia mercati meteo (v8.0: rilassato filtro price>0.85)
  - `weather_feed.py` — feed previsioni meteo multi-provider
  - `finbert_feed.py` — feed FinBERT/VADER per NLP sentiment analysis
  - `strategies/` — strategie di trading
    - `arb_gabagool.py` — DISABILITATO v9.1: exploit incrementNonce() (settlement non atomico)
    - `event_driven.py` — news-reactive + sentiment (v8.0: CATEGORY_CONFIG per-categoria)
    - `high_prob_bond.py` — obbligazioni ad alta prob (v8.0: politics boost, sports hard blacklist)
    - `market_making.py` — DISABILITATO v7.0 (necessita $2K+ budget)
    - `whale_copy.py` — copy trading (v8.0: size-aware filtering, v9.2.2: migrazione endpoint data-api.polymarket.com)
  - `validators/` — Layer 2: Signal Validator (v9.0)
    - `signal_validator.py` — UnifiedSignal, SignalReport, 8 gate checks (edge, confidence, resolution, liquidity, spread, EV net-of-fees, DA, VPIN) (v10.1: Gate 6 semplificato, MIN_CONFIDENCE 0.50)
    - `devils_advocate.py` — Contraddittorio deterministico (sport blacklist, edge sospetto, overconfident, volume basso, losing streak)
    - `signal_converter.py` — Adattatori da ogni strategia a UnifiedSignal (from_event/bond/whale/prediction/weather_opportunity)
  - `monitoring/` — Layer 5: Feedback Loop (v9.0)
    - `attribution.py` — AttributionEngine: P&L per segnale, Brier score, alpha decay
    - `drift_detector.py` — DriftDetector: concept drift (win rate calo >30%), microstructure drift (spread)
    - `calibration.py` — CalibrationEngine: suggerimenti min_edge e kelly_fraction basati su Brier/alpha/MC (v10.0: check Monte Carlo)
    - `empirical_kelly.py` — EmpiricalKelly: Monte Carlo bootstrap 10K paths, CV_edge haircut, cache 1h (v10.0)
  - `storage/` — Layer 0: Persistenza (v9.0, opzionale)
    - `database.py` — PostgreSQL: tabelle trades, market_snapshots, calibration_log, drift_alerts
    - `redis_bus.py` — Redis Pub/Sub + cache con fallback in-memory
  - `agents/` — Layer 1: Orchestrator (v9.0)
    - `orchestrator.py` — OrchestratorAgent: prioritizza mercati per volume/prezzo/anomaly, routing a strategie
  - `risk/` — Layer 3: Risk avanzato (v9.0)
    - `correlation_monitor.py` — CorrelationMonitor: max 40% capitale per tema (politics, crypto, weather, etc.)
    - `tail_risk.py` — TailRiskAgent: VaR 95%, max loss scenario, posizioni concentrate
  - `execution/` — Layer 4: Execution Engine (v9.0)
    - `execution_agent.py` — ExecutionAgent: LIMIT_MAKER (<=\$30), TWAP tranche \$15/2s (>\$30)
  - `migrate_json_to_pg.py` — Script migrazione one-shot JSON → PostgreSQL
  - `utils/gdelt_feed.py` — client GDELT API v2 (news globali + tone, gratuito, v9.2.2: rate limit 10s, query semplici, circuit breaker escalante)
  - `utils/vpin_monitor.py` — VPIN toxic flow detection (Easley, Lopez de Prado, O'Hara 2012) v9.2.1
  - `utils/finlight_feed.py` — client Finlight API v2 (news + sentiment, v9.2.2: exponential backoff su 429)
  - `utils/binance_feed.py` — feed multi-crypto Binance WS (v9.2.2: backoff esponenziale con jitter, stale detection)
  - `utils/redeemer.py` — auto-redeem posizioni risolte via Safe proxy (v10.0.1: CTF-only routing, firma v=1; v9.2.3: Data API redeemable, strict conditionId)
  - `utils/risk_manager.py` — Kelly sizing (v10.1: spread dinamico pmxt, kurtosis haircut condizionale, empirical Kelly blend), triple barrier, stop-loss cooldown, correlation check, flash move + VPIN v9.2.1
  - `utils/whale_profiler.py` — Profiler wallet whale (whitelist automatica)
  - `.claude/rules/` — regole modulari per strategia (event-driven, risk, merge, general)
  - `.claude/agents/` — agenti custom (becker-analyst, strategy-debugger)
  - `.claude/commands/` — slash commands (/backtest, /strategy-status, /pnl-report)
- `/root/finlight_feed.py` — client Finlight API v2 (news + sentiment)
- `/root/becker-dataset/data/` — Becker Dataset (115M trade Polymarket per analisi)
- `/root/polymarket_strategy/` — analisi/config strategia crypto scalper
  - `analyze_strategy.py`
  - `strategy_config.json`

## Strategie e allocazione (v9.2.1)
| Strategia      | Allocazione | Descrizione                                    |
|----------------|-------------|------------------------------------------------|
| high_prob_bond | 30%         | Bond ad alta prob (politics boost +0.12)       |
| data_driven    | 30%         | Prediction data-driven (v9.2.1: -5% da 35%)   |
| weather        | 20%         | Mercati meteo (MAX_WEATHER_BET=$15 v9.1)       |
| event_driven   | 15%         | News-reactive + NLP edge (v9.2.1: +5% da 10%) |
| whale_copy     | 5%          | Copy trading size-aware (segnali rari)         |
| arb_gabagool   | 0%          | DISABILITATO v9.1: exploit incrementNonce()    |
| arbitrage      | 0%          | DISABILITATO v9.1: exploit incrementNonce()    |
| crypto_5min    | 0%          | ELIMINATO v7.0: Kelly negativo, fees 3.15%     |
| market_making  | 0%          | ELIMINATO v7.0: necessita $2K+ budget          |


## Modifiche v10.2.0 (6 fix profittabilita')
### Fix P0: Segnali general data_driven disabilitati
- `_analyze_general_market()` ritorna None immediatamente
- YES+NO!=1.0 e' quasi sempre quote staleness, non mispricing reale
- Segnale contrarian senza ancoraggio fondamentale
- 30% allocazione ora va interamente alla sotto-strategia crypto (edge reale)

### Fix P0: Whale win_rate fallback corretto
- Wallet con PnL>0 ma no wins/losses: fallback 0.58→0.62 (sopra soglia MIN_WHALE_WIN_RATE)
- Wallet senza dati verificabili: fallback 0.57→0.0 (rifiutato)
- API exception: fallback 0.57→0.0 (rifiutato)
- Ora solo wallet con dati reali o PnL positivo confermato passano il filtro

### Fix P1: EmpiricalKelly MIN_TRADES 30→15
- Riduce il periodo di undersizing sistematico (kurtosis haircut 50%)
- Il blend 70/30 (empirical/prior) compensa il rumore con meno trade
- Attivazione in ~1-2 settimane invece di 4-8

### Fix P2: Weather smart_buy
- buy_market() (taker) sostituito con smart_buy() (maker-first)
- Meno slippage sugli ordini weather

### Fix P2: ExecutionAgent TODO
- Marcato per collegamento futuro (TWAP per trade >0)
- Attualmente tutti i trade passano direttamente a smart_buy()

### Fix P2: Orchestrator SKIP filtering
- Mercati classificati SKIP (volume < 100) vengono ora esclusi da shared_markets
- Riduce cicli di calcolo inutili su mercati dormienti

## Modifiche v10.1.0 (10 bug critici profitability + spread dinamico pmxt)
### Bug fix profitability (10 fix)
1. **Doppio fee counting Signal Validator** (`signal_validator.py`): Gate 6 sottraeva fee da edge già net-of-fees → `ev = signal.edge` direttamente
2. **win_prob sbagliata nel Kelly** (`bot.py`): passava `ev.confidence` (qualità segnale 0.5-0.9) invece di probabilità di vincita → ora `min(price + edge, 0.95/0.99)`
3. **Doppia correzione fat-tail** (`risk_manager.py`): kurtosis haircut 0.50x + Empirical Kelly = doppia correzione → kurtosis applicato SOLO se `emp_factor is None`
4. **NO-bias filter bloccava weather** (`risk_manager.py`): weather compra YES >0.80 legittimamente → aggiunto `"weather"` alle esenzioni
5. **Edge gate proxy sbagliato** (`risk_manager.py`): `abs(0.5 - price)` non è edge reale, filtrava trade legittimi → commentato (Kelly + validator EV gate coprono)
6. **Weather fee errata** (`weather.py`): usava fee 0.005 ma weather markets sono fee-free su Polymarket → `fee = 0.0`
7. **MAX_BET_SIZE env default** (`config.py`): default "25" non allineato con v8.0 → cambiato a "40"
8. **Bond certainty_score > 1.0** (`high_prob_bond.py`): politics+finance boost poteva eccedere 1.0 → `return min(score, 1.0)`
9. **Segnali REVIEW scartati** (`bot.py`): segnali REVIEW con edge alto venivano silenziosamente ignorati → accettati se `edge >= 0.04` con log
10. **Confidence gate troppo restrittivo** (`signal_validator.py`): `MIN_CONFIDENCE` 0.60→0.50 (soft gate: 1 fail→REVIEW, non SKIP)
### Spread dinamico pmxt data-driven
- **Fonte dati**: pmxt orderbook archive (`archive.pmxt.dev`), 500K+ price_change events, 500 mercati (Feb 2026)
- **`_estimate_spread_cost(price)`** in `risk_manager.py`: sostituisce hardcoded `spread_cost = 0.005`
- Calibrazione per zona di prezzo (mediana spread effettivo / 2 per exit taker):
  - Bond 93-100c: 0.005 (invariato), High 80-93c: 0.010, **Mid 20-80c: 0.020** (era 0.005, 4x sottostimato), Longshot <20c: 0.010
- Usato in `kelly_size()` e nel `Fees/Vol gate` di `can_trade()`
- Impatto: Kelly sizing più accurato per weather/event-driven nella mid-range (meno trade con payoff inflato)

## Modifiche v10.0 (Empirical Kelly con Monte Carlo)
### Empirical Kelly — position sizing data-driven
- **EmpiricalKelly** (`monitoring/empirical_kelly.py`): sostituisce Kelly fractions statiche con haircut derivato da bootstrap resampling
- Formula: `f_empirical = 1 - CV_edge`, dove `CV_edge = std(path_means) / mean(path_means)` su 10K paths MC
- Bootstrap: `(10000, n_trades)` indici random con replacement, wealth curves via log1p→cumsum→exp
- **DD95**: 95th percentile max drawdown per path (running max → drawdown → percentile)
- Cache 1h, ricalcolo ogni 500 cicli o 10 nuovi trade chiusi, minimo 30 trade per attivare
### Blend 70/30 in kelly_size()
- `base_frac = base_frac * (0.70 * emp_factor + 0.30)` — 30% statico come prior bayesiano
- Evita che CV_edge rumoroso (pochi trade) porti base_frac a zero
- Se < 30 trade o cache scaduta → fallback sizing statico v9.x (nessuna modifica)
### CalibrationEngine Check MC
- Check 3 in `analyze()`: se `f_empirical < 0.50` suggerisce riduzione Kelly, se `> 0.85` (n>=50) suggerisce +10%
- Mostra CV_edge e DD95 nel suggerimento
### Fallback e sicurezza
- numpy mancante → `empirical_kelly = None`, sizing statico
- < 30 trade → `update()` ritorna None, sizing statico
- Cache >2h → `get_adjustment_factor()` ritorna None, sizing statico
- CV_edge = 1.0 (edge <= 0) → `f_empirical = 0.0`, blend → `base_frac * 0.30` (floor 30%)
- Eccezione in MC → try/except in bot.py, log warning, sizing statico
### Performance
- numpy vectorized, 10K paths x 100 trade = ~27ms

## Modifiche v10.0.1 (Fix redeemer GS013)
### Root cause
- Il Safe proxy Polymarket (`0x22051C50...`) con implementazione custom (`0xE51abdf8...`) ha un bug ABI: `execTransaction` reverta con GS013 ("Signatures data too short") per certi indirizzi target (NegRiskAdapter) con data non-vuoto
- Tutti e 3 i mercati falliti erano `negRisk=True` → routing via NRA → GS013
- L'encoding ABI era corretto (verificato byte per byte), il problema è nel decoder del Safe custom
### Fix 1: CTF-only routing
- **Rimosso branch neg_risk → NRA**: tutte le redemption passano direttamente per CTF (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`)
- `CTF.redeemPositions()` gestisce tutte le condizioni incluse neg_risk — verificato via simulazione on-chain
### Fix 2: Firma v=1 (msg.sender == owner)
- **Rimossa firma ECDSA**: sostituita con signature v=1 (`r=owner_address, s=0x00, v=1`)
- In Safe v1.3.0 `checkNSignatures`, v=1 verifica `msg.sender == currentOwner` — nessun signing necessario quando l'EOA (owner) invia la TX
- Rimossi: `getTransactionHash`, `defunct_hash_message`, logica di signing
### Verifica
- Simulazione eth_call OK per tutti e 3 i mercati falliti (1389999, 1413735, 1413736)
- Redemption manuale on-chain riuscita per tutti e 3

## Modifiche v9.2.1 (Stoikov + riallocazione post-Gemchange)
### VPIN Toxic Flow Detection
- **VPINMonitor** (`utils/vpin_monitor.py`): implementa VPIN (Easley, Lopez de Prado, O'Hara 2012) per detectare informed trading
- Gate #8 nel Signal Validator: blocca trade se VPIN >= 0.7 (toxic flow)
- Integrato in `risk_manager.can_trade()` come check aggiuntivo
- Feed dati dal WS price feed via callback `on_trade`
### VAMP Pricing (Stoikov)
- **VAMP** (Volume Adjusted Mid Price) in `polymarket_ws_feed.py`: prezzo mid pesato per quantita' bid/ask
- Formula: `(best_bid * ask_qty + best_ask * bid_qty) / (bid_qty + ask_qty)`
- Fallback a mid-price classico se mancano le quantita'
- Sostituisce il mid-price semplice in tutti gli eventi WS (book_update, price_change)
### Flash Move Protection
- Detecta movimenti di prezzo rapidi (>5c in 60s) che indicano informed trading o manipolazione
- Integrato in `risk_manager.can_trade()`: blocca trade su mercati in flash move
- Tracking tramite `_price_history` (deque maxlen=20) su ogni token WS
### Riallocazione post-Gemchange
- **data_driven** 35→30%: edge floor 0.060 sospetto (artificialmente inflato), diversificazione
- **event_driven** 10→15%: NLP edge (FinBERT/GDELT) non dipende da latenza, politics piu' profittevole (Becker: +$18.6M PnL)
- Motivazione: articolo Gemchange documenta compressione margini arb (finestra 12.3s→2.7s), competizione e' su velocita' non su analisi fondamentale

## Modifiche v9.2.3 (Data API redeemable + strict conditionId)
### Data API redeemable fast-path (P0)
- **`fetch_redeemable_positions()`** in `redeemer.py`: singola query a `data-api.polymarket.com/positions?user=` ritorna tutte le posizioni con flag `redeemable`
- **`_find_resolved_markets()`** riscritta: Data API come fonte primaria, Gamma API come fallback per mercati non trovati
- **`_check_resolved_markets()` in `bot.py`**: fast-path Data API prima del loop Gamma per-market — 1 chiamata vs N sequenziali
- Se Data API trova tutti i mercati risolti, skippa completamente il loop Gamma (zero API calls extra)
- Se Data API fallisce o non trova tutti, fallback al loop Gamma classico (zero regressione)
- Matching posizioni via conditionId, slug, o title
### Strict conditionId validation (P1a)
- **Rimosso `zfill(64)`** in `_redeem_position()`: un conditionId troncato indica bug nella fonte dati, non va paddato silenziosamente
- **Validazione strict**: esattamente 64 hex chars (32 bytes) o return False con log `[REDEEM] conditionId malformato`
- Ispirato dal tipo `B256` del Polymarket CLI Rust che rifiuta qualsiasi valore != 32 bytes
- Rimossa validazione ridondante post-padding (ora la validazione avviene prima dell'encoding)

## Modifiche v9.2.2 (Feed reliability + Redeemer fix)
### Redeemer TX reverted (P0)
- **conditionId padding**: pad a 32 bytes (bytes32) con `zfill(64)` — causa principale del revert (Gamma API ritornava conditionId troncato)
- **Gas estimation**: `estimate_gas()` con 30% buffer, fallback a 500K se estimation fallisce
- **Retry on revert**: fino a 3 tentativi con gas incrementato +50% ad ogni retry
- **Log migliorato**: gasUsed/gasLimit per diagnosi, validazione pre-redeem
### GDELT rate limit fix (P0/P1)
- **Query semplici**: 1 query singola per categoria (era 2 combinate con OR che causavano timeout sistematici)
- **Intervallo 10s** tra richieste (era 5.5s, insufficiente per IP flaggati)
- **Timeout 30s** (era 20s per query combinate)
- **200 text-body non conta come errore**: GDELT ritorna 200 con "Please limit requests..." — gestito senza incrementare errori circuit breaker
- **Circuit breaker escalante**: cooldown 60s→180s→300s→600s in base al numero di trip (era fisso 300s)
### Finlight exponential backoff (P1)
- **Backoff esponenziale** su 429: 30s→60s→120s→240s→300s (cap), con counter e timestamp per retry
- **Backoff check** prima di ogni fetch — evita richieste inutili durante backoff
- **Reset su successo**: counter azzerato dopo risposta 200
### Whale API endpoint migration (P0)
- **Endpoint migrato**: `gamma-api.polymarket.com` → `data-api.polymarket.com` (vecchio ritornava 404)
- **Parametro aggiornato**: `address=` → `user=` per endpoint `/activity`
- **Filtro trade type**: solo BUY/TRADE (skip REDEEM, SELL)
- **Profiles endpoint** migrato a `data-api.polymarket.com/profiles/`
### Binance WS stability (P2)
- **Backoff esponenziale con jitter**: 2s→4s→8s→16s→max 30s su ConnectionClosed, con random jitter 30%
- **ping_timeout=30s, close_timeout=10s** espliciti (era solo ping_interval=20)
- **Tracking disconnessioni**: contatore consecutivo, reset su connessione riuscita
- **`is_stale()`**: metodo per detection dati vecchi (nessun messaggio da >60s)

## Modifiche v9.0.0 (Architettura Agentica a 6 Layer)
### Layer 2: Signal Validator + Devil's Advocate
- **SignalValidator** con 8 gate checks: min edge (>=0.02), confidence (>=50% v10.1), resolution clarity (<30gg), liquidita' (>=2x size), spread (<=5%), EV positivo net-of-fees (v10.1), Devil's Advocate, VPIN toxic flow (v9.2.1)
- **DevilsAdvocate** fast-path: sport blacklist per bond, edge sospetto (>0.20 non-arb), overconfident senza news, volume <$500, losing streak (3+)
- **SignalConverter**: adattatori da ogni strategia a UnifiedSignal normalizzato
- Arb gabagool e arbitrage DISABILITATI v9.1 (exploit incrementNonce())
- Integrato nel main loop: ogni strategia passa dal validator prima di execute()

### Layer 5: Monitoring & Feedback Loop
- **AttributionEngine**: traccia ogni trade entry→exit con Brier score, alpha decay per strategia
- **DriftDetector**: allarme se win rate recente cala >30% vs storico, monitoring spread
- **CalibrationEngine**: suggerisce aumento min_edge se Brier >0.35, riduzione Kelly se alpha <0.50, check MC v10.0
- **EmpiricalKelly** (v10.0): Monte Carlo bootstrap 10K paths per Kelly fraction data-driven, ricalcolo ogni 500 cicli
- Attribution registrata dopo ogni close_trade(), drift/calibration/empirical kelly analizzati ogni 500 cicli

### Layer 0: PostgreSQL + Redis Storage
- **Database** PostgreSQL: tabelle trades, market_snapshots, calibration_log, drift_alerts con indici
- **EventBus** Redis: Pub/Sub su 6 canali + cache con TTL, fallback in-memory
- Graceful degradation: se PG/Redis non configurati, il bot usa JSON come v8.1
- Env vars: `DATABASE_DSN`, `REDIS_URL` (opzionali)
- Script `migrate_json_to_pg.py` per migrazione one-shot

### Layer 1: Orchestrator Agent
- **OrchestratorAgent**: classifica mercati per priorita' (CRITICAL/HIGH/MEDIUM/LOW/SKIP)
- CRITICAL: volume spike >3x, arb opportunity
- HIGH: prezzo >0.93 o <0.07 (bond candidate)
- SKIP: volume <100 (dormiente)
- Routing intelligente: CRITICAL/HIGH → tutte le strategie, MEDIUM → bond+data, LOW → solo data

### Layer 3: Correlation Monitor + Tail Risk
- **CorrelationMonitor**: max 40% capitale per tema (politics, crypto, weather, geopolitical, sports, finance)
- Classificazione automatica per keyword da question/category/tags
- Integrato in `can_trade()` del risk manager
- **TailRiskAgent**: VaR 95% con approssimazione normale, max loss scenario, posizioni concentrate (>10% capitale)
- Analisi ogni 200 cicli, alert CRITICAL se max loss >50% capitale

### Layer 4: Execution Engine
- **ExecutionAgent**: LIMIT_MAKER per trade <=\$30, TWAP per trade >\$30
- TWAP: tranche da \$15 ogni 2s con slippage check tra tranche
- Max slippage 2%, stop automatico se superato

## Modifiche v9.1.0 (Security: exploit incrementNonce + bugfix)
### Sicurezza: arb disabilitate
- **arb_gabagool** e **arbitrage** disabilitate (allocazione 0%) per exploit `incrementNonce()` sul CTF Exchange
- L'exploit permette di invalidare il settlement on-chain dopo il match CLOB, lasciando l'arb bot con posizione naked
- Le gambe dell'arb sono eseguite in sequenza (gap 3-5s), non atomicamente — finestra di attacco
- Nessuna verifica on-chain post-trade nel codice attuale
- 30% riallocato: bond +5%, event +10%, weather +5%, whale +5%, data +5%
### Weather sizing
- **MAX_WEATHER_BET = $15** (weather.py): cap specifico per weather, loss da $25 troppo pesanti vs win medi ~$12
### Bugfix
- **Weather confidence UnboundLocalError**: variabile usata prima della definizione (432 errori in 15h)
- **v9.0 logging**: aggiunto `force=True` a `logging.basicConfig()` — layer v9.0 non loggavano
- **GDELT circuit breaker auto-reset**: aggiunto cooldown 5 min (prima era permanente fino a restart)
- **Type annotations mypy**: 17 errori risolti in storage/redis_bus.py, storage/database.py, risk/correlation_monitor.py, monitoring/calibration.py

## Modifiche v8.0.0 (Becker Dataset optimization)
### Strategie
- **Bond**: Politics boost +0.12 certainty, Finance boost +0.08, Sports→HARD blacklist (Becker: -$17.4M PnL sport), MIN_PROB 0.90 per politics (era 0.93)
- **Whale copy**: MIN_WHALE_WIN_RATE 0.55→0.60, confidence ridotta per mega-whale >$100K (×0.70) e micro <$100 (×0.50), copy fraction adattiva (5%/8%/10%)
- **Weather**: Price >0.85 permesso se edge>=0.05 & confidence>=0.75, MIN_EDGE 0.02 per same-day
- **Event-driven**: CATEGORY_CONFIG con min_edge/confidence_boost per-categoria (politics 0.02 + boost 0.10, crypto_regulatory 0.05, geopolitical 0.04)
- **Arb gabagool**: Fee reale p*(1-p)*0.0625 per TUTTI i mercati (non piu' flat 0.25%), profit/fee ratio gate (rifiuta se fee > 50% profitto)
### Allocazione
- event_driven 10→15% (politics e' la categoria piu' profittevole)
- whale_copy 5→10% (Becker: whale $1K-$100K hanno 68.4% WR)
- data_driven 10→5% (crypto ben calibrato, poco edge)
- weather 20→15% (redistribuito dove c'e' piu' edge)
- max_bet_size $25→$40 (Becker sweet spot: $100-$1K)
### Bug fix: Stop-Loss Loop
- **Bid sanity check** (bot.py): se bid < 50% dell'entry price, HOLD (order book vuoto)
- **Stop-loss cooldown** (risk_manager.py): dopo stop loss, mercato bloccato per 4h
- Risolve bug dove bond comprava/vendeva in loop sullo stesso mercato perdendo $12.50 ogni 30min

## Modifiche v8.1.0 (GDELT + Claude Code config)
- **GDELT feed** (`utils/gdelt_feed.py`): fonte news complementare a Finlight, gratuita, copertura superior su political/geopolitical
- **Merge multi-fonte** in event_driven: sceglie la fonte con segnale piu' forte per categoria, +10% boost se concordano, MAI doppio conteggio
- **`.claude/rules/`**: 4 regole modulari attivate per glob (event-driven, risk, merge, strategy-general)
- **`.claude/agents/`**: becker-analyst (analisi dataset), strategy-debugger (diagnosi)
- **`.claude/commands/`**: /backtest, /strategy-status, /pnl-report

## Modifiche v7.0.0
- Kelly sizing proporzionale (rimosso floor fisso $50 che annullava Kelly)
- Anti-hedging: blocco posizioni opposte sullo stesso mercato
- Exposure limit: max 15% capitale per singolo mercato
- max_bet_size ridotto da $50 a $35
- Whale copy delay ridotto da 300s a 120s
- Eliminati crypto_5min e market_making (performance negativa)

## Stack tecnico
- Python, requests, asyncio, numpy
- API: Polymarket CLOB, Gamma API, Data API, Finlight v2, GDELT v2, Binance, LunarCrush, CryptoQuant, Nansen
- NLP: FinBERT (ProsusAI/finbert) con fallback VADER
- Storage: PostgreSQL + Redis (opzionali, graceful degradation a JSON + in-memory)
- Chain: Polygon (chain_id 137)
- Paper trading attivo di default

## Note importanti
- Il file `.env` contiene chiavi private e API keys — mai leggerlo o mostrarlo
- Il bot ha un risk manager integrato con Kelly criterion (proporzionale per strategia, v10.0: haircut empirico MC)
- La strategia event_driven usa Finlight + GDELT per news sentiment in tempo reale (merge multi-fonte)
- Il risk manager ha stop-loss cooldown (4h) per evitare loop distruttivi
- Il Signal Validator filtra trade a bassa qualita' PRIMA dell'esecuzione (8 gate checks, incluso VPIN v9.2.1). Gate 6 (EV) usa edge direttamente (net-of-fees, v10.1). MIN_CONFIDENCE=0.50 (v10.1). Segnali REVIEW con edge>=0.04 vengono accettati (v10.1)
- VPIN monitor blocca trade su mercati con toxic flow (informed trading >= 0.7)
- Flash move protection blocca trade su mercati con price velocity > 5c/60s
- VAMP (Volume Adjusted Mid Price) sostituisce mid-price semplice nel WS feed
- Le strategie arb (gabagool, arbitrage) sono DISABILITATE per exploit incrementNonce() — NON riabilitare senza verifica settlement on-chain atomica
- Il Correlation Monitor limita esposizione per tema (max 40% capitale)
- Il Drift Detector segnala cali di win rate >30% vs storico
- L'Empirical Kelly (v10.0) richiede numpy e almeno 30 trade chiusi per strategia — sotto questa soglia usa sizing statico v9.x
- Il redeemer auto-riscuote vincite da mercati risolti via Safe proxy — SEMPRE via CTF direttamente, MAI via NRA (v10.0.1: bypass bug GS013 del Safe custom)
- Il redeemer usa firma v=1 (msg.sender == owner) invece di ECDSA — piu' robusto con il Safe proxy Polymarket (v10.0.1)
- conditionId DEVE essere esattamente 64 hex chars (v9.2.3: strict validation, no padding)
- Il redeemer usa Data API (`data-api.polymarket.com/positions`) come fonte primaria per detectare posizioni redeemable (v9.2.3), Gamma API come fallback
- GDELT usa query semplici (1 per categoria) con intervallo 10s e circuit breaker escalante (v9.2.2)
- Finlight ha exponential backoff su 429 (30s→300s cap) — API key potrebbe avere quota limitata
- Whale copy usa `data-api.polymarket.com` (NON gamma-api, ritorna 404 dal 2026)
- Binance WS ha backoff esponenziale con jitter su disconnessione (v9.2.2)
- Lo spread_cost nel Kelly sizing è dinamico per zona di prezzo (v10.1: pmxt data-driven). NON riportare a hardcoded 0.005
- Weather markets sono fee-free su Polymarket (v10.1): `fee = 0.0` in weather.py. NON aggiungere fee
- Kurtosis haircut si applica SOLO se Empirical Kelly non è attivo (v10.1). NON applicare entrambi insieme
- Becker Dataset in `/root/becker-dataset/data/` — fonte delle ottimizzazioni v8.0
- pmxt orderbook archive in `archive.pmxt.dev/Polymarket` — fonte calibrazione spread v10.1 (download: `/dumps/polymarket_orderbook_YYYY-MM-DDTHH.parquet`)
- Setup PG+Redis: `apt install postgresql redis-server && pip install psycopg2-binary redis`
- Env vars storage: `DATABASE_DSN=postgresql://localhost/polymarket_bot`, `REDIS_URL=redis://localhost:6379`
- Avvio: `python bot.py` (paper) / `python bot.py --live` (reale). Per live serve `echo 'CONFERMO' | python3 bot.py --live`
- **Balance/allowance**: se il log mostra `not enough balance / allowance` (HTTP 400 dal CLOB), il wallet proxy Safe non ha USDC sufficienti o l'allowance verso il CTF Exchange è scaduta. Verificare: (1) saldo USDC sul Safe proxy su Polygonscan, (2) allowance ERC-20 verso `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` (CTF Exchange). Il bot continua a girare e riprova al ciclo successivo
- Repo GitHub: https://github.com/ludovicocapuano-dev/polymarket-bot (privato)
