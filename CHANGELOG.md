# Changelog — Polymarket Multi-Strategy Trading Bot

*Changelog dettagliato del bot. Per la documentazione corrente, vedi [CLAUDE.md](CLAUDE.md)*

---

## Modifiche v10.5 (Glint.trade Real-Time Intelligence Feed)
### Glint.trade Integration (`utils/glint_feed.py`, NUOVO)
- **GlintFeed**: WebSocket client async per `wss://api.glint.trade/ws` — segnali news/social pre-matchati su contratti Polymarket
- **Auth a 2 livelli**: session JWT (`GLINT_SESSION_TOKEN` env var, ~7gg dal browser) → WS JWT (~5min, auto-refresh ogni 4min via `GET /api/auth/ws-token`)
- **Two-phase correlation**: messaggi `type:new` (segnale) + `type:related_markets` (mercati matchati) correlati via `feed_item_id` con timeout 30s
- **Filtro qualità**: solo segnali con `relevance_score >= 5` (scala 1-10 AI)
- **Mapping categorie**: Glint categories → `CATEGORY_CONFIG` keys (political, crypto_regulatory, geopolitical, macro, tech)
- **Inferenza sentiment**: keyword-based da `impact_reason` + `title` (positive/negative keywords → score [-0.8, +0.8])
- **Output duale**: `drain_opportunities()` (ricco, market match pre-computed) + interfaccia Finlight/GDELT-compatibile (`get_event_sentiment()`, `get_news_strength()`, `detect_breaking_news()`)
- **Graceful degradation**: senza `GLINT_SESSION_TOKEN` = feed disabilitato (noop), 401 = token scaduto + disabilitato per sessione, senza `websockets` = noop
- **Backoff esponenziale con jitter**: 2s→30s su disconnessione (pattern binance_feed.py)
### Event-Driven Integration (`strategies/event_driven.py`)
- **`__init__`**: parametro `glint: GlintFeed | None = None`
- **`_check_glint_opportunities()`** (NUOVO): drain queue Glint → cross-ref con shared_markets (condition_id/slug/question) → edge da `strength × 0.15 × price_discount` → confidence 0.58 base + relevance boost + impact boost → `EventOpportunity(signal_type="glint_reactive")`
- **`scan()`**: step 0 Glint-reactive prima di news-reactive, deduplica per market_id
- **`_merge_breaking_news()`**: Glint come terza fonte, bonus 1.2x, min_articles=1
- **`_get_merged_news_strength()`**: `max(f_str, g_str, gl_str)`, boost 10% se 2+ concordano
- **`execute()`**: `glint_reactive` trattato come `news_reactive` (cooldown 180s, size boost 1.3x, timeout 8s)
### Bot Integration (`bot.py`)
- Import `GlintFeed`, init `self.glint_feed = GlintFeed()`, injection in EventDrivenStrategy, `glint_feed.connect()` in `asyncio.gather()`
### Env var
- `GLINT_SESSION_TOKEN`: session JWT dal browser Glint.trade (~7gg). Senza = feed disabilitato, bot gira normalmente

## Modifiche v10.4 (On-Chain Monitor + Copy SELL + Retry + Partial Fill + P&L Dashboard)
### P&L Dashboard (`bot.py`)
- **`_log_pnl_report()`**: report periodico ogni 50 cicli con breakdown per strategia, capitale, esposizione, unrealized P&L
- **`_log_position_health()`**: health check ogni 100 cicli con ages, profitto/perdita corrente, concentrazione per strategia
- **CLAUDE.md refactor**: da 515 righe a ~130 righe core, changelog spostato in CHANGELOG.md

### On-Chain Monitor (`utils/onchain_monitor.py`, NUOVO)
- **WebSocket Polygon** block-by-block: sottoscrive blocchi via `web3.LegacyWebSocketProvider` (web3 v7), polling ~1s
- **Decode `matchOrders` calldata**: ABI 13-campo Order struct, selector `0x2287e350`
- **Contratti monitorati**: CTF Exchange (`0x4bFb41d5B...`), Neg Risk Adapter (`0xC5d563A...`), Operator (`0xd91E80cF...`)
- **Latenza ~2s** vs 120s del polling HTTP attuale
- **Graceful degradation**: se `web3` non installato, stub class no-op (bot non crasha)
- **Riconnessione automatica**: backoff esponenziale 5s→60s, max 10 tentativi, rotazione round-robin tra endpoint WSS
- **Endpoint WSS gratuiti integrati**: PublicNode (`wss://polygon-bor-rpc.publicnode.com`) + DRPC (`wss://polygon.drpc.org`) — nessun API key richiesto
- **Integrazione**: `OnChainMonitor.add_callback(whale_copy.on_chain_trade)` in `bot.py`
- **Env var**: `POLYGON_WSS` (opzionale) — URL WebSocket nodo Polygon premium. Se non configurata, usa endpoint gratuiti integrati
### Copy SELL trades (`strategies/whale_copy.py`)
- **Rimosso filtro** `elif side == "SELL": continue` — ora copia anche le vendite dei whale
- **`WhaleTrade.action`**: nuovo campo `"BUY"` o `"SELL"` per distinguere l'azione
- **`execute()`**: branch BUY/SELL — SELL usa `smart_sell()`, BUY usa `smart_buy()` con A-S
- **Paper SELL**: PnL inverso (`won = random() < (1 - sim_win_prob)`)
- **SELL sizing**: copy fraction fissa (no Kelly — Kelly non ha senso per exit)
### Retry con price increment
- **`MAX_COPY_RETRIES = 3`**: fino a 3 retry dopo ordine fallito
- **`RETRY_PRICE_INCREMENT = 0.01`**: +1¢/retry per BUY, -1¢/retry per SELL
- **Delay**: `min(1s × attempt, 5s)` tra retry
- **Cloudflare detection**: se risposta contiene "cloudflare"/"attention required", skip retry immediato
### Partial fill handling
- Se `size_matched < size * 0.99` → partial fill: log, cancella residuo, registra filled_size
- Trade comunque contato come eseguito (partial è meglio di nulla)
### Order history tracking
- **`CopyOrderHistory`** dataclass: timestamp, whale_name, market_id, side, action, attempts, status, error
- **`_order_history`**: lista ultimi 100 tentativi, accessibile via `get_order_history()`
- **Status**: PENDING, SUCCESS, PARTIAL, FAILED, CLOUDFLARE
### On-chain hook
- **`on_chain_trade(trade_event)`**: callback per `OnChainMonitor`, aggiunge a coda pending
- **`_detect_whale_trades()`**: processa coda on-chain prima del polling HTTP
- **Zero dipendenze nuove** sul path whale_copy (on-chain monitor è opzionale)
### USDC Auto-Approval (`utils/redeemer.py`)
- **`check_and_approve_usdc()`** in `Redeemer`: controlla allowance USDC del Safe proxy verso 3 contratti CLOB
- **Contratti**: CTF Exchange (`0x4bFb41d5B...`), Neg Risk CTF Exchange (`0xC5d563A...`), Operator (`0xd91E80cF...`)
- **Soglia**: se allowance < $1000 USDC → approve `max_uint256` via Safe `execTransaction`
- **Chiamato al startup** da `bot.py` dopo init redeemer
- **Logga** lo stato: `[APPROVE] 0xABC... allowance OK: $X USDC` o `approvato!`

## Modifiche v10.3 (Avellaneda-Stoikov Optimal Execution)
### Modello A-S adattato per prediction markets [0,1]
- **`utils/avellaneda_stoikov.py`** (NUOVO): modulo standalone con matematica A-S
- **Reservation price**: `r = s − q × γ × σ²τ` (shift down per inventario esistente)
- **Optimal half-spread**: `δ/2 = γ_spread × σ²τ / 2 + vpin_premium` (semi-spread con adverse selection)
- **Optimal bid**: `bid = r − δ/2`, clippato a `[best_bid, min(mid, target)]`
- **Varianza binaria**: `σ²τ = p(1−p)` (varianza naturale del binary outcome — max a p=0.5, zero a 0 e 1)
- **Due γ separati**: `GAMMA_INVENTORY=0.30` (inventory skew) e `GAMMA_SPREAD=0.05` (half-spread)
- **γ scalato per volume 24h** (proxy di κ): liquido (≥$10K) → γ×0.7, illiquido (≤$1K) → γ×1.5, interpolazione lineare
- **VPIN premium**: 0-2¢ per adverse selection (proporzionale a VPIN, complementa il gate VPIN≥0.7 nel validator)
- Il termine κ `(2/γ)·ln(1+γ/κ)` è omesso: produce spread troppo larghi per prezzi [0,1]
### Integrazione in smart_buy()
- **`polymarket_api.py`**: 3 parametri opzionali aggiunti a `smart_buy()`: `inventory_frac`, `volume_24h`, `vpin` (default 0.0, backward compatible)
- Branch A-S nel path `spread ≤ 0.10` quando almeno un parametro > 0, altrimenti behavior naive invariato
- Log `[AS-EXEC]` con bid, naive e delta per monitoraggio
### Call sites aggiornati (5 strategie)
- **`high_prob_bond.py`**, **`event_driven.py`**, **`data_driven.py`**, **`whale_copy.py`**, **`weather.py`**: calcolo `inventory_frac` e `vpin` prima di `smart_buy()`
- Pattern: `market_inventory_frac(open_trades, market_id, budget)` + `vpin_monitor.get_vpin(market_id)`
### Fix P0: weather.py target_price mancante
- `smart_buy(token_id, size)` → `smart_buy(token_id, size, target_price=price, ...)` — parametro obbligatorio mancante dalla v10.2
### Impatto
- Aggiustamenti 0-2¢ che riducono l'aggressività con inventario alto o toxic flow
- NON modifica behavior per trade senza inventario/VPIN (parametri default a 0.0 → path naive)
- Se fill rate cala troppo (A-S troppo conservativo), abbassare `GAMMA_SPREAD` in `avellaneda_stoikov.py`

## Modifiche v10.2.0 (GARCH + CVaR + Portfolio VaR + 6 fix profittabilità)
### GARCH(1,1) + Exponential Weighting (MIT 18.S096 Lectures 7+9)
- **`_recent_volatility()`** in `risk_manager.py`: MAD sostituita con GARCH(1,1)
- Formula: `σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1}` (α=0.06 shock, β=0.93 persistenza)
- ω calibrato su varianza incondizionata: `ω = var * (1 - α - β)`
- Media calcolata con exponential weighting λ=0.94 (RiskMetrics, Abbott)
- Cattura volatility clustering: dopo un loss grande la vol resta alta → Kelly riduce size automaticamente
- Forward-looking: predice vol del prossimo periodo, non solo misura il passato

### CVaR — Expected Shortfall (MIT 18.S096 Lecture 14)
- **`cvar_95`** aggiunto a `TailRiskReport` in `tail_risk.py`
- Formula normale: `CVaR = μ_loss + σ_loss · φ(z) / (1-α)` dove φ(1.645)=0.10314
- CVaR cattura la **severità** della coda (media delle perdite oltre VaR), non solo la soglia
- Warning se CVaR95 > 40% capitale
- Loggato ogni 200 cicli accanto al VaR

### Portfolio VaR con matrice di covarianza (MIT 18.S096 Lecture 7)
- **`portfolio_var()`** in `correlation_monitor.py`: `VaR = z · √(w^T · Σ · w)`
- σ_i per binary outcome: `size · √(p · (1-p))`
- Σ stimata con correlazioni per tema:
  - Stesso tema (rho_intra=0.40): elections correlano con elections
  - Temi diversi (rho_inter=0.10): elections poco correlate con weather
- **Diversification ratio**: `portfolio_var / sum_individual_var` — misura il beneficio della diversificazione
- **`portfolio_cvar()`**: Expected Shortfall del portafoglio (`σ_p · φ(z) / (1-α)`)
- Loggato ogni 200 cicli con n_positions e diversification ratio

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

## Modifiche v10.2.1 (Fix race condition redeem)
### Root cause
- Data API reporta `redeemable=true` ~24 secondi prima che `reportPayouts` sia minato on-chain
- Il redeem reverta con GS013 (Safe proxy) perché il CTF non ha ancora il payout settato
- Il trade veniva chiuso internamente e aggiunto a `_resolved_cache` → mai ritentato
- Token on-chain restavano bloccati permanentemente

### Fix: Pre-check payoutDenominator + retry
- **`_redeem_position()`** in `redeemer.py`: controlla `payoutDenominator` sul CTF prima del redeem
  - Se `== 0`: condizione non risolta on-chain → ritorna `None` (non `False`)
  - Se pre-check fallisce (RPC error): procede comunque (gas estimation catturerà errori)
- **`bot.py`** (Data API path + Gamma path): se redeem ritorna `None`:
  - NON chiude il trade da `open_trades`
  - NON aggiunge a `_resolved_cache`
  - Mercato ritentato automaticamente al ciclo successivo (~30s)
- Distinzione: `None` = "non ancora risolvibile, ritenta" vs `False` = "TX fallita, chiudi trade"
- Type hint aggiornato: `_redeem_position() -> bool | None`

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
- Cache 1h, ricalcolo ogni 500 cicli o 10 nuovi trade chiusi, minimo 15 trade per attivare (v10.2: ridotto da 30)
### Blend 70/30 in kelly_size()
- `base_frac = base_frac * (0.70 * emp_factor + 0.30)` — 30% statico come prior bayesiano
- Evita che CV_edge rumoroso (pochi trade) porti base_frac a zero
- Se < 30 trade o cache scaduta → fallback sizing statico v9.x (nessuna modifica)
### CalibrationEngine Check MC
- Check 3 in `analyze()`: se `f_empirical < 0.50` suggerisce riduzione Kelly, se `> 0.85` (n>=50) suggerisce +10%
- Mostra CV_edge e DD95 nel suggerimento
### Fallback e sicurezza
- numpy mancante → `empirical_kelly = None`, sizing statico
- < 15 trade (v10.2: era 30) → `update()` ritorna None, sizing statico
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
- v10.2: **Portfolio VaR** con matrice di covarianza (`z · √(w^T·Σ·w)`), correlazioni intra-tema 0.40, inter-tema 0.10
- v10.2: **Portfolio CVaR** (Expected Shortfall) e **diversification ratio**
- **TailRiskAgent**: VaR 95% + CVaR 95% (v10.2: Expected Shortfall), max loss scenario, posizioni concentrate (>10% capitale)
- Analisi ogni 200 cicli, alert CRITICAL se max loss >50% capitale, warning se CVaR95 > 40% capitale

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
