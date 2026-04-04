# Preferenze
- Rispondi sempre in italiano
- Nessuna convenzione particolare di codice

# Regole di Engagement

## Prima di ogni sessione (OBBLIGATORIO)
1. `ps aux | grep bot.py` — verifica bot running e nessun duplicato
2. Leggi log recente: `ls -t logs/bot_*.log | head -1`
3. Controlla errori: `grep ERROR $LOG | tail -5`
4. **LEGGI TUTTI questi file memoria:**
   - `MEMORY.md` — stato corrente, strategie, parametri
   - `SESSION_LOG.md` — COSA È STATO FATTO nelle ultime 5 sessioni (evita duplicati!)
   - `FILE_INVENTORY.md` — mappa file, import, copie duplicate
   - `CHANGELOG.md` — ogni modifica recente con file:linea
   - Path: `/root/.claude/projects/-root/memory/`

## Prima di ogni modifica al codice
1. **Leggere FILE_INVENTORY.md** — verificare quale copia il bot usa
2. Leggere il file PRIMA di modificarlo
3. Verificare che il diff sia corretto
4. **Pulire __pycache__**: `find . -type d -name __pycache__ -exec rm -rf {} +`
3. Se modifica parametri trading: giustificare con dati (log, Becker, backtest)
4. Se modifica filtri weather: simulare con `python3 backtest_replay.py --compare`
5. Consultare mistakes.md per non ripetere errori noti

## Prima di riavviare il bot
1. `ps aux | grep bot.py` — kill istanze esistenti
2. Nessun .env esposto o credenziali in chiaro
3. `echo 'CONFERMO' | python3 bot.py --live`
4. Controllare primi 30s di log per errori di import/init

## Agenti specializzati disponibili
- **pnl-analyst**: analisi PnL con contesto storico, breakdown per strategia, raccomandazioni
- **strategy-optimizer**: ottimizzazione parametri con simulazione what-if su dati storici
- **market-scanner**: scoperta nuove opportunita', gap nella copertura, nuove citta'

## Comandi disponibili
- `/health-check` — stato rapido del bot (processo, errori, scanning)
- `/post-mortem` — analisi fine giornata con aggiornamento memoria
- `/pnl-report` — report PnL dettagliato per strategia
- `/risk-check` — stato risk manager, correlazioni, circuit breaker
- `/portfolio` — panoramica posizioni aperte
- `/scan` — scan mercati attivi
- `/backtest` — backtest strategia
- `/strategy-status` — stato strategie attive

## Backtest replay
- `python3 backtest_replay.py --compare` — confronta filtri vecchi vs nuovi
- `python3 backtest_replay.py --min-edge 0.10 --min-confidence 0.60` — testa parametri custom
- MAI proporre modifiche parametri senza aver prima simulato l'impatto

# Progetto: Polymarket Multi-Strategy Trading Bot (v10.5)
Repo: https://github.com/ludovicocapuano-dev/polymarket-bot (privato)
Bot automatico di trading su Polymarket con 5 strategie attive (4 eliminate per performance negativa o sicurezza).
v8.0 ottimizzato con analisi Becker Dataset (115M trade, 381K mercati risolti).
v9.0: architettura agentica a 6 layer. v10.0: Empirical Kelly MC. v10.2: GARCH+CVaR.
v10.3: Avellaneda-Stoikov optimal execution. v10.4: on-chain monitor, copy SELL, P&L dashboard.
v10.5: Glint.trade real-time intelligence feed per event_driven.

## Architettura v9.0 — 6 Layer

| Layer | Componente | Descrizione |
|-------|-----------|-------------|
| Layer 2 | Signal Validator + Devil's Advocate | 8 gate checks pre-esecuzione (v10.1: EV net-of-fees, REVIEW accepted, MIN_CONF 0.50), contraddittorio deterministico |
| Layer 5 | Attribution + Drift + Calibration + Empirical Kelly | Brier score, concept drift, suggerimenti parametri, MC position sizing (v10.0) |
| Layer 0 | PostgreSQL + Redis | Storage persistente + event bus (opzionale, graceful degradation) |
| Layer 1 | Orchestrator Agent | Prioritizzazione mercati (CRITICAL/HIGH/MEDIUM/LOW/SKIP) |
| Layer 3 | Correlation Monitor + Tail Risk | Max 40% per tema, Portfolio VaR/CVaR con covarianza (v10.2), worst-case analysis |
| Layer 4 | Execution Engine | TWAP per trade >$30, LIMIT_MAKER con A-S optimal bid (v10.3) |

## Struttura
- `/root/polymarket_toolkit/` — codice principale del bot
  - `bot.py` — entry point, orchestratore multi-strategia, position manager, integrazione 6 layer v9.0
  - `config.py` — configurazione centralizzata (da .env) + db_dsn/redis_url v9.0
  - `.env` — credenziali e parametri (NON toccare/leggere)
  - `crypto_5min.py` — DISABILITATO v7.0 (fees > edge)
  - `weather.py` — strategia mercati meteo (v8.0: rilassato filtro price>0.85, v10.3: smart_buy A-S + fix target_price mancante)
  - `weather_feed.py` — feed previsioni meteo multi-provider
  - `finbert_feed.py` — feed FinBERT/VADER per NLP sentiment analysis
  - `strategies/` — strategie di trading
    - `arb_gabagool.py` — DISABILITATO v9.1: exploit incrementNonce() (settlement non atomico)
    - `event_driven.py` — news-reactive + sentiment (v8.0: CATEGORY_CONFIG per-categoria)
    - `high_prob_bond.py` — obbligazioni ad alta prob (v8.0: politics boost, sports hard blacklist)
    - `market_making.py` — DISABILITATO v7.0 (necessita $2K+ budget)
    - `whale_copy.py` — copy trading (v10.4: copy SELL, retry con increment, partial fill, Cloudflare detection, on-chain hook)
  - `validators/` — Layer 2: Signal Validator (v9.0)
    - `signal_validator.py` — UnifiedSignal, SignalReport, 8 gate checks
    - `devils_advocate.py` — Contraddittorio deterministico
    - `signal_converter.py` — Adattatori da ogni strategia a UnifiedSignal
  - `monitoring/` — Layer 5: Feedback Loop (v9.0)
    - `attribution.py` — AttributionEngine: P&L per segnale, Brier score, alpha decay
    - `drift_detector.py` — DriftDetector: concept drift, microstructure drift
    - `calibration.py` — CalibrationEngine: suggerimenti min_edge e kelly_fraction
    - `empirical_kelly.py` — EmpiricalKelly: Monte Carlo bootstrap 10K paths, CV_edge haircut
  - `storage/` — Layer 0: Persistenza (v9.0, opzionale)
    - `database.py` — PostgreSQL: tabelle trades, market_snapshots, calibration_log, drift_alerts
    - `redis_bus.py` — Redis Pub/Sub + cache con fallback in-memory
  - `agents/` — Layer 1: Orchestrator (v9.0)
    - `orchestrator.py` — OrchestratorAgent: prioritizza mercati per volume/prezzo/anomaly
  - `risk/` — Layer 3: Risk avanzato (v9.0)
    - `correlation_monitor.py` — CorrelationMonitor: max 40% per tema + Portfolio VaR/CVaR (v10.2)
    - `tail_risk.py` — TailRiskAgent: VaR 95% + CVaR 95% (Expected Shortfall, v10.2)
  - `execution/` — Layer 4: Execution Engine (v9.0)
    - `execution_agent.py` — ExecutionAgent: LIMIT_MAKER (<=\$30), TWAP tranche \$15/2s (>\$30)
  - `migrate_json_to_pg.py` — Script migrazione one-shot JSON → PostgreSQL
  - `utils/gdelt_feed.py` — client GDELT API v2 (news globali + tone, gratuito)
  - `utils/vpin_monitor.py` — VPIN toxic flow detection (Easley, Lopez de Prado, O'Hara 2012)
  - `utils/finlight_feed.py` — client Finlight API v2 (news + sentiment)
  - `utils/glint_feed.py` — client Glint.trade WS (real-time intelligence + market matching, v10.5)
  - `utils/twitter_feed.py` — client Twitter/X via twscrape (breaking news + VADER sentiment, v10.6)
  - `utils/binance_feed.py` — feed multi-crypto Binance WS
  - `utils/redeemer.py` — auto-redeem posizioni risolte via Safe proxy + USDC auto-approval
  - `utils/avellaneda_stoikov.py` — A-S optimal execution per prediction markets [0,1] (v10.3)
  - `utils/risk_manager.py` — Kelly sizing (GARCH+empirical Kelly), triple barrier, stop-loss cooldown, correlation check
  - `utils/whale_profiler.py` — Profiler wallet whale (whitelist automatica)
  - `utils/onchain_monitor.py` — Monitor on-chain Polygon WebSocket, decode matchOrders (~2s latency, v10.4)
  - `utils/whale_backtest.py` — Monte Carlo multi-scenario backtest framework
  - `utils/replication_score.py` — L1 distance replication scoring
  - `utils/clickhouse_analytics.py` — ClickHouse schema + writer con graceful degradation
  - `.claude/rules/` — regole modulari per strategia (event-driven, risk, merge, general)
  - `.claude/agents/` — agenti custom (becker-analyst, strategy-debugger)
  - `.claude/commands/` — slash commands (/backtest, /strategy-status, /pnl-report)
- `/root/becker-dataset/data/` — Becker Dataset (115M trade Polymarket per analisi)

## Strategie e allocazione (v10.8.4)
| Strategia          | Allocazione | Descrizione                                    |
|--------------------|-------------|------------------------------------------------|
| weather            | 90%         | v10.8.4: tail selling + forecast divergence, min_edge 5/12/20%, EV>=0.10, uncertainty penalty |
| resolution_sniper  | 10%         | v10.8: Resolution sniping UMA, quasi risk-free |
| negrisk_arb        | indip.      | v10.8.4: NegRisk sum arb scanner, MAX_SIZE=$100, opera indipendentemente dal budget |
| holding_rewards    | indip.      | v10.8.4: 4% APY su mercati long-term eligible, $20/mercato, scan ogni 10 cicli |
| favorite_longshot  | indip.      | v10.8.4: bias favorite-longshot, $25/trade, fee-free markets, edge 2-6% |
| event_driven       | 0%          | v10.8: DISABILITATO — WR 0%, -$350, feed rotti |
| high_prob_bond     | 0%          | v10.8: DISABILITATO — asimmetria payoff 1:17, -$55 |
| whale_copy         | 0%          | v10.8: DISABILITATO — 0 trade mai eseguiti     |
| data_driven        | 0%          | PAUSATO v10.6: WR 42.9% vs break-even 67%     |
| arb_gabagool       | 0%          | DISABILITATO v9.1: exploit incrementNonce()    |
| arbitrage          | 0%          | DISABILITATO v9.1: exploit incrementNonce()    |
| crypto_5min        | 0%          | ELIMINATO v7.0: Kelly negativo, fees 3.15%     |
| market_making      | 0%          | ELIMINATO v7.0: necessita $2K+ budget          |

## Stack tecnico
- Python, requests, asyncio, numpy
- API: Polymarket CLOB, Gamma API, Data API, Finlight v2, GDELT v2, Glint.trade WS, Weather Underground, Binance, LunarCrush, CryptoQuant, Nansen
- NLP: FinBERT (ProsusAI/finbert) con fallback VADER
- Storage: PostgreSQL + Redis (opzionali, graceful degradation a JSON + in-memory)
- Chain: Polygon (chain_id 137)
- Paper trading attivo di default

## Note importanti
- Il file `.env` contiene chiavi private e API keys — mai leggerlo o mostrarlo
- Il bot ha un risk manager integrato con Kelly criterion (proporzionale per strategia, v10.0: haircut empirico MC, v10.2: volatilità GARCH(1,1))
- La strategia event_driven usa Finlight + GDELT + Glint.trade + Twitter/X per news sentiment in tempo reale (merge multi-fonte 4 sorgenti)
- Twitter/X feed via API v2 Bearer Token ($200/mese Basic plan). VADER sentiment, confidence cap 0.80, strength discount 15%. Env var: TWITTER_BEARER_TOKEN. Senza token = noop. File: `utils/twitter_feed.py`
- Weather Underground è la fonte di settlement per i weather markets Polymarket (v10.8). Peso 2.0 (massimo) nel consensus multi-provider. API: api.weather.com v3/v1. Env var: WUNDERGROUND_API_KEY (free con PWS). Senza key = provider disabilitato (noop). File: `utils/weather_feed.py`
- Glint.trade fornisce segnali pre-matchati su contratti Polymarket con relevance_score AI (1-10). WS auth a 2 livelli: session JWT (GLINT_SESSION_TOKEN, ~7gg dal browser) → WS JWT (~5min, auto-refresh). Senza token = feed disabilitato (noop). 401 = token scaduto, disabilitato per sessione
- Il risk manager ha stop-loss cooldown (4h) per evitare loop distruttivi
- Il Signal Validator filtra trade a bassa qualita' PRIMA dell'esecuzione (8 gate checks, incluso VPIN v9.2.1). Gate 6 (EV) usa edge direttamente (net-of-fees, v10.1). MIN_CONFIDENCE=0.50 (v10.1). Segnali REVIEW con edge>=0.04 vengono accettati (v10.1)
- VPIN monitor blocca trade su mercati con toxic flow (informed trading >= 0.7)
- Flash move protection blocca trade su mercati con price velocity > 5c/60s
- VAMP (Volume Adjusted Mid Price) sostituisce mid-price semplice nel WS feed
- Le strategie arb (gabagool, arbitrage) sono DISABILITATE per exploit incrementNonce() — NON riabilitare senza verifica settlement on-chain atomica
- Il Correlation Monitor limita esposizione per tema (max 40% capitale) e calcola Portfolio VaR/CVaR con matrice di covarianza (v10.2)
- Il Drift Detector segnala cali di win rate >30% vs storico
- L'Empirical Kelly (v10.0) richiede numpy e almeno 15 trade chiusi per strategia (v10.2: ridotto da 30) — sotto questa soglia usa sizing statico v9.x
- La volatilità nel Kelly sizing usa GARCH(1,1) con exponential weighting λ=0.94 (v10.2: MIT 18.S096). NON riportare a MAD
- Il CVaR (Expected Shortfall) nel tail_risk cattura la severità della coda, non solo la soglia VaR (v10.2: MIT 18.S096)
- Il Portfolio VaR usa correlazioni stimate per tema (rho_intra=0.40, rho_inter=0.10). Diversification ratio < 1.0 indica beneficio della diversificazione
- Il redeemer auto-riscuote vincite da mercati risolti via Safe proxy — SEMPRE via CTF direttamente, MAI via NRA (v10.0.1: bypass bug GS013 del Safe custom)
- Il redeemer usa firma v=1 (msg.sender == owner) invece di ECDSA — piu' robusto con il Safe proxy Polymarket (v10.0.1)
- conditionId DEVE essere esattamente 64 hex chars (v9.2.3: strict validation, no padding)
- Il redeemer usa Data API (`data-api.polymarket.com/positions`) come fonte primaria per detectare posizioni redeemable (v9.2.3), Gamma API come fallback
- Il redeemer pre-verifica `payoutDenominator` on-chain prima del redeem (v10.2.1). Se la condizione non è risolta, ritorna `None` → bot riprova al ciclo successivo. Race condition Data API vs chain ~24s
- GDELT usa query semplici (1 per categoria) con intervallo 10s e circuit breaker escalante (v9.2.2)
- Finlight ha exponential backoff su 429 (30s→300s cap) — API key potrebbe avere quota limitata
- Whale copy usa `data-api.polymarket.com` (NON gamma-api, ritorna 404 dal 2026)
- Whale copy v10.4 copia sia BUY che SELL dei whale. SELL usa `smart_sell()`, BUY usa `smart_buy()` con A-S. NON re-introdurre il filtro `elif side == "SELL": continue`
- On-chain monitor (`utils/onchain_monitor.py`) usa `web3.LegacyWebSocketProvider` (web3 v7). Ha endpoint WSS gratuiti integrati (PublicNode, DRPC). `POLYGON_WSS` env var opzionale per endpoint premium. Senza web3, graceful degradation a polling HTTP
- USDC auto-approval (`redeemer.check_and_approve_usdc()`) controlla allowance verso CTF Exchange, Neg Risk CTF Exchange, Operator. Se < $1000 → approve max_uint256 via Safe proxy. Chiamato al startup
- On-chain monitor decode `matchOrders` calldata (selector `0x2287e350`) dai 3 contratti Polymarket: CTF Exchange, Neg Risk Adapter, Operator
- Retry copy trade: max 3 tentativi con +1¢/retry (BUY) o -1¢/retry (SELL). Se Cloudflare block, skip retry immediato
- Binance WS ha backoff esponenziale con jitter su disconnessione (v9.2.2)
- L'execution usa Avellaneda-Stoikov per calcolare il bid ottimale in `smart_buy()` (v10.3). Parametri A-S sono opzionali e backward compatible — se tutti a 0.0, usa il path naive `best_bid + TICK`. I due γ (GAMMA_INVENTORY=0.30, GAMMA_SPREAD=0.05) sono scalati per volume 24h. NON rimuovere il clipping `[best_bid, min(mid, target)]` — previene bid fuori range
- Lo spread_cost nel Kelly sizing è dinamico per zona di prezzo (v10.1: pmxt data-driven). NON riportare a hardcoded 0.005
- Weather markets sono fee-free su Polymarket (v10.1): `fee = 0.0` in weather.py. NON aggiungere fee
- Latency Hunter (v10.8.5): `_model_update_loop` in bot.py monitora rilascio GFS/ECMWF e forza re-scan. `weather_feed.invalidate_cache()` svuota cache e salva forecast precedente per shift detection. Shift >= 1°C → priority scan. Finestre: GFS ~03:30/09:30/15:30/21:30 UTC, ECMWF ~06:00/18:00 UTC
- Weather v10.8.4: BUY_NO solo su bin con P(YES)<15% (tail selling). BUY_YES solo su bin cheap con forecast forte (forecast divergence). Uncertainty penalty nella confidence (σ>4→-30%, σ>2.5→-15%). NON rilassare questi filtri senza dati
- NegRisk arb (`strategies/negrisk_arb.py`): scanner sum deviation su mercati multi-outcome. Se SUM(YES)≠$1.00 oltre 2%, esegue buy_all o sell_all. MAX_ARB_SIZE=$100, cooldown 30min. Opera indipendentemente dal budget allocato
- Kelly sizing v10.8.4: uncertainty-adjusted (`risk_manager.py` riga 381-401). sigma per strategia (weather=0.08, sniper=0.03). Per weather sigma cresce con orizzonte: 0.05+days*0.02. NON rimuovere uncertainty_factor
- Holding Rewards (`strategies/holding_rewards.py`): compra posizioni in 13 mercati eligible per 4% APY. Keywords: 2028 presidential, midterm, Putin, Xi, Erdogan, Netanyahu, Zelensky. $20/mercato, scan orario
- Favorite-Longshot Bias (`strategies/favorite_longshot.py`): compra favoriti $0.70-$0.90 in mercati fee-free ad alto volume (>$50K). Edge stimato 2-6% (NBER). $25/trade, max 10 posizioni, quarter-Kelly. Esclude weather/crypto
- Kurtosis haircut si applica SOLO se Empirical Kelly non è attivo (v10.1). NON applicare entrambi insieme
- Becker Dataset in `/root/becker-dataset/data/` — fonte delle ottimizzazioni v8.0
- pmxt orderbook archive in `archive.pmxt.dev/Polymarket` — fonte calibrazione spread v10.1 (download: `/dumps/polymarket_orderbook_YYYY-MM-DDTHH.parquet`)
- Setup PG+Redis: `apt install postgresql redis-server && pip install psycopg2-binary redis`
- Env vars storage: `DATABASE_DSN=postgresql://localhost/polymarket_bot`, `REDIS_URL=redis://localhost:6379`
- Avvio: `python bot.py` (paper) / `python bot.py --live` (reale). Per live serve `echo 'CONFERMO' | python3 bot.py --live`
- **Balance/allowance**: se il log mostra `not enough balance / allowance` (HTTP 400 dal CLOB), il wallet proxy Safe non ha USDC sufficienti o l'allowance verso il CTF Exchange è scaduta. Verificare: (1) saldo USDC sul Safe proxy su Polygonscan, (2) allowance ERC-20 verso `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` (CTF Exchange). Il bot continua a girare e riprova al ciclo successivo
- Repo GitHub: https://github.com/ludovicocapuano-dev/polymarket-bot (privato)

> Per changelog dettagliato vedi [CHANGELOG.md](CHANGELOG.md)
