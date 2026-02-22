# Preferenze
- Rispondi sempre in italiano
- Nessuna convenzione particolare di codice

# Progetto: Polymarket Multi-Strategy Trading Bot (v9.0.0)
Repo: https://github.com/ludovicocapuano-dev/polymarket-bot (privato)
Bot automatico di trading su Polymarket con 7 strategie attive (2 eliminate per performance negativa).
v8.0 ottimizzato con analisi Becker Dataset (115M trade, 381K mercati risolti).
v8.1: integrazione GDELT feed per event_driven + configurazione .claude/ (rules, agents, commands).
v9.0: architettura agentica a 6 layer (Signal Validator, Monitoring, Storage, Orchestrator, Risk, Execution).

## Architettura v9.0 — 6 Layer

| Layer | Componente | Descrizione |
|-------|-----------|-------------|
| Layer 2 | Signal Validator + Devil's Advocate | 7 gate checks pre-esecuzione, contraddittorio deterministico |
| Layer 5 | Attribution + Drift + Calibration | Brier score, concept drift detection, suggerimenti parametri |
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
    - `arb_gabagool.py` — arbitraggio combinatorio (v8.0: fee reale + profit/fee gate)
    - `event_driven.py` — news-reactive + sentiment (v8.0: CATEGORY_CONFIG per-categoria)
    - `high_prob_bond.py` — obbligazioni ad alta prob (v8.0: politics boost, sports hard blacklist)
    - `market_making.py` — DISABILITATO v7.0 (necessita $2K+ budget)
    - `whale_copy.py` — copy trading (v8.0: size-aware filtering, copy fraction adattiva)
  - `validators/` — Layer 2: Signal Validator (v9.0)
    - `signal_validator.py` — UnifiedSignal, SignalReport, 7 gate checks (edge, confidence, resolution, liquidity, spread, EV, DA)
    - `devils_advocate.py` — Contraddittorio deterministico (sport blacklist, edge sospetto, overconfident, volume basso, losing streak)
    - `signal_converter.py` — Adattatori da ogni strategia a UnifiedSignal (from_event/bond/whale/prediction/weather_opportunity)
  - `monitoring/` — Layer 5: Feedback Loop (v9.0)
    - `attribution.py` — AttributionEngine: P&L per segnale, Brier score, alpha decay
    - `drift_detector.py` — DriftDetector: concept drift (win rate calo >30%), microstructure drift (spread)
    - `calibration.py` — CalibrationEngine: suggerimenti min_edge e kelly_fraction basati su Brier/alpha
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
  - `utils/gdelt_feed.py` — client GDELT API v2 (news globali + tone, gratuito, v8.1)
  - `utils/risk_manager.py` — Kelly sizing, triple barrier, stop-loss cooldown, correlation check v9.0
  - `utils/whale_profiler.py` — Profiler wallet whale (whitelist automatica)
  - `.claude/rules/` — regole modulari per strategia (event-driven, risk, merge, general)
  - `.claude/agents/` — agenti custom (becker-analyst, strategy-debugger)
  - `.claude/commands/` — slash commands (/backtest, /strategy-status, /pnl-report)
- `/root/finlight_feed.py` — client Finlight API v2 (news + sentiment)
- `/root/becker-dataset/data/` — Becker Dataset (115M trade Polymarket per analisi)
- `/root/polymarket_strategy/` — analisi/config strategia crypto scalper
  - `analyze_strategy.py`
  - `strategy_config.json`

## Strategie e allocazione (v8.0.0)
| Strategia      | Allocazione | Descrizione                                    |
|----------------|-------------|------------------------------------------------|
| high_prob_bond | 25%         | Bond ad alta prob (politics boost +0.12)       |
| arb_gabagool   | 20%         | Arbitraggio combinatorio (fee reale p*(1-p)*6.25%) |
| event_driven   | 15%         | News-reactive + CATEGORY_CONFIG + GDELT merge  |
| weather        | 15%         | Mercati meteo (rilassato price>0.85 se edge forte) |
| arbitrage      | 10%         | Arbitraggio classico + cross-platform          |
| whale_copy     | 10%         | Copy trading size-aware (sweet spot $1K-$100K) |
| data_driven    | 5%          | Prediction data-driven (crypto ben calibrato)  |
| crypto_5min    | 0%          | ELIMINATO: Kelly negativo, fees 3.15%          |
| market_making  | 0%          | ELIMINATO: necessita $2K+ budget               |

## Modifiche v9.0.0 (Architettura Agentica a 6 Layer)
### Layer 2: Signal Validator + Devil's Advocate
- **SignalValidator** con 7 gate checks: min edge (>=0.02), confidence (>=60%), resolution clarity (<30gg), liquidita' (>=2x size), spread (<=5%), EV positivo post-fee, Devil's Advocate
- **DevilsAdvocate** fast-path: sport blacklist per bond, edge sospetto (>0.20 non-arb), overconfident senza news, volume <$500, losing streak (3+)
- **SignalConverter**: adattatori da ogni strategia a UnifiedSignal normalizzato
- Arb gabagool e arbitrage BYPASSANO il validator (profitto deterministico)
- Integrato nel main loop: ogni strategia passa dal validator prima di execute()

### Layer 5: Monitoring & Feedback Loop
- **AttributionEngine**: traccia ogni trade entry→exit con Brier score, alpha decay per strategia
- **DriftDetector**: allarme se win rate recente cala >30% vs storico, monitoring spread
- **CalibrationEngine**: suggerisce aumento min_edge se Brier >0.35, riduzione Kelly se alpha <0.50
- Attribution registrata dopo ogni close_trade(), drift/calibration analizzati ogni 500 cicli

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
- Python, requests, asyncio
- API: Polymarket CLOB, Gamma API, Finlight v2, GDELT v2, Binance, LunarCrush, CryptoQuant, Nansen
- NLP: FinBERT (ProsusAI/finbert) con fallback VADER
- Storage: PostgreSQL + Redis (opzionali, graceful degradation a JSON + in-memory)
- Chain: Polygon (chain_id 137)
- Paper trading attivo di default

## Note importanti
- Il file `.env` contiene chiavi private e API keys — mai leggerlo o mostrarlo
- Il bot ha un risk manager integrato con Kelly criterion (proporzionale per strategia)
- La strategia event_driven usa Finlight + GDELT per news sentiment in tempo reale (merge multi-fonte)
- Il risk manager ha stop-loss cooldown (4h) per evitare loop distruttivi
- Il Signal Validator filtra trade a bassa qualita' PRIMA dell'esecuzione (arb bypassano)
- Il Correlation Monitor limita esposizione per tema (max 40% capitale)
- Il Drift Detector segnala cali di win rate >30% vs storico
- Becker Dataset in `/root/becker-dataset/data/` — fonte delle ottimizzazioni v8.0
- Setup PG+Redis: `apt install postgresql redis-server && pip install psycopg2-binary redis`
- Env vars storage: `DATABASE_DSN=postgresql://localhost/polymarket_bot`, `REDIS_URL=redis://localhost:6379`
- Avvio: `python bot.py` (paper) / `python bot.py --live` (reale)
- Repo GitHub: https://github.com/ludovicocapuano-dev/polymarket-bot (privato)
