# Analisi Test Coverage — Polymarket Bot v9.2.2

## Stato attuale

### Test esistenti
Il codebase ha **5 file di test**, tutti nella root del progetto:

| File | Tipo | Descrizione |
|------|------|-------------|
| `test_trades_endpoint.py` | Script manuale | Verifica risposta CLOB /trades per un token |
| `test_trades_endpoint2.py` | Script manuale | Varianti endpoint /trades (cursor, filtri, py_clob_client) |
| `test_trades_endpoint3.py` | Script manuale | Paginazione trade e ricerca token falliti |
| `test_trades_endpoint4.py` | Script manuale | Ricerca fill via maker_address nei maker_orders |
| `test_fill_response.py` | Script manuale | Struttura risposta CLOB post-ordine |

**Problemi critici:**
- Nessuno di questi e' un test automatico (no pytest, no unittest, no assert)
- Tutti richiedono credenziali reali (leggono `.env` e autenticano su CLOB)
- Non sono eseguibili in CI/CD
- Coprono solo un aspetto: debug dell'endpoint `/trades`
- Non esiste nessuna directory `tests/`, nessun `conftest.py`, nessun `pytest.ini`

### Copertura per modulo

| Modulo | File | LOC | Test unitari | Priorita' |
|--------|------|-----|:------------:|:---------:|
| **validators/** | `signal_validator.py` | 184 | 0 | **P0** |
| **validators/** | `devils_advocate.py` | 101 | 0 | **P0** |
| **validators/** | `signal_converter.py` | 146 | 0 | **P1** |
| **utils/** | `risk_manager.py` | 722 | 0 | **P0** |
| **utils/** | `vpin_monitor.py` | 249 | 0 | **P0** |
| **monitoring/** | `attribution.py` | 194 | 0 | **P1** |
| **monitoring/** | `drift_detector.py` | 178 | 0 | **P1** |
| **monitoring/** | `calibration.py` | 130 | 0 | **P1** |
| **risk/** | `correlation_monitor.py` | 135 | 0 | **P1** |
| **risk/** | `tail_risk.py` | 130 | 0 | **P1** |
| **execution/** | `execution_agent.py` | 239 | 0 | **P2** |
| **agents/** | `orchestrator.py` | 194 | 0 | **P2** |
| **config.py** | - | 153 | 0 | **P1** |
| **strategies/** | 5 strategie attive | ~2500+ | 0 | **P2** |
| **utils/** | 12+ feed/api | ~3000+ | 0 | **P3** |
| **storage/** | `database.py`, `redis_bus.py` | ~400 | 0 | **P3** |

**Copertura totale stimata: ~0%** (nessun test automatico esiste).

---

## Proposte di miglioramento — ordinate per impatto/rischio

### P0 — Critici (logica che protegge il capitale)

#### 1. `SignalValidator.validate()` — 8 gate checks
**Perche':** E' l'ultimo filtro prima dell'esecuzione di un trade. Un bug qui = soldi persi.

Test da scrivere:
- Segnale che passa tutti i gate -> `ValidationResult.TRADE`
- Segnale con edge < 0.02 -> `SKIP`
- Segnale con confidence < 60% -> `SKIP`
- Segnale con days_to_resolution > 30 -> `SKIP`
- Segnale con days_to_resolution sconosciuto (-1) -> gate saltato (pass)
- Segnale con liquidita' insufficiente (< 2x trade_size) -> `SKIP`
- Segnale con spread > 5% -> `SKIP`
- Segnale con EV negativo dopo fee round-trip -> `SKIP`
- Segnale flaggato dal Devil's Advocate -> `SKIP`
- Segnale con VPIN >= 0.7 (toxic flow) -> `SKIP`
- Segnale con 1 solo failure e score >= 0.7 -> `REVIEW`
- Verifica che score = passed / (passed + failed)

#### 2. `DevilsAdvocate.challenge()` — contraddittorio deterministico
**Perche':** Previene trade su sport (Becker: -$17.4M PnL), edge sospetti, overconfidence.

Test da scrivere:
- Bond su mercato sport ("NBA playoffs") -> flagged con "Sport blacklist"
- Edge > 0.20 su non-arb -> flagged con "Edge sospetto"
- Confidence > 0.85 con news_strength < 0.3 su signal_type non-bond -> flagged
- Volume < $500 -> flagged
- Losing streak >= 3 -> flagged
- Segnale legittimo -> NOT flagged
- Bond su mercato politics -> NOT flagged (niente sport keyword)
- Edge > 0.20 su arb_gabagool -> NOT flagged (arb esente)

#### 3. `RiskManager.can_trade()` — 12+ checks pre-trade
**Perche':** E' il guardiano del capitale. Bug critici passati (stop-loss loop, $12.50 persi ogni 30min).

Test da scrivere:
- Halt globale -> blocked
- Halt per strategia -> blocked (altre strategie continuano)
- Daily loss > max -> halt globale
- Consecutive losses >= max -> halt strategia
- Max open positions raggiunto -> blocked
- Reserve floor violated -> blocked
- Budget strategia esaurito -> blocked
- Size > max_bet_size -> blocked
- Size < $1 -> blocked
- Longshot filter: YES @$0.15 -> blocked
- NO-bias filter: YES @$0.85 (non bond) -> blocked
- NO-bias filter: YES @$0.95 (bond) -> ALLOWED (esenzione)
- Stop-loss cooldown: mercato stop-lossato < 4h fa -> blocked
- Stop-loss cooldown: mercato stop-lossato > 4h fa -> allowed
- Anti-hedging: BUY_YES quando gia' BUY_NO aperto -> blocked
- Anti-stacking: BUY_YES quando gia' BUY_YES aperto -> blocked
- Exposure limit: > 15% capitale su un mercato -> blocked
- Edge gate: edge stimato < 2x costo round-trip -> blocked
- Fees/Vol gate: fee > 30% volatilita' attesa -> blocked
- Flash move protection: mercato in flash move -> blocked
- VPIN toxic flow: mercato tossico -> blocked
- Correlation monitor: tema > 40% capitale -> blocked
- Trade legittimo con tutti i check OK -> allowed

#### 4. `RiskManager.kelly_size()` — Kelly Criterion
**Perche':** Determina quanto capitale viene messo a rischio. Il bug del floor fisso $50 (risolto v7.0) ha causato over-betting sistematico.

Test da scrivere:
- win_prob=0.7, price=0.5 -> size > 0 ragionevole
- win_prob=0.3, price=0.5 -> size = 0 (Kelly negativo)
- price=0 o price=1 -> size = 0
- Capitale < $30 (floor) -> size = 0
- Weather same-day (days_ahead=0) -> fraction boosted
- Strategia in drawdown > 30% budget -> size dimezzata
- CPPI scaling con daily_pnl negativo -> size ridotta
- Optimal f cap: nessun trade > 20% del budget
- Grossman-Zhou cushion: size scala con distanza dal floor

#### 5. `VPINMonitor` / `MarketVPIN` — toxic flow detection
**Perche':** Blocca trade su mercati manipolati. Errore nel calcolo VPIN = falsi negativi (trade su mercati tossici) o falsi positivi (opportunita' perse).

Test da scrivere:
- Nessun trade registrato -> VPIN = 0.0
- Trade tutti nella stessa direzione (toxic) -> VPIN alto
- Trade bilanciati buy/sell -> VPIN basso
- Volume bucket overflow (trade grande che riempie 2+ bucket) -> corretto
- `_normal_cdf(0)` = 0.5
- `_normal_cdf(3)` ~ 0.999
- `_estimate_sigma()` con < 3 prezzi -> 0.0
- `check_toxicity()` con VPIN >= 0.7 -> (True, reason)
- `check_toxicity()` per mercato non tracciato -> (False, "")

#### 6. `RiskManager.check_barrier()` — Triple Barrier Exit
**Perche':** Decide quando chiudere posizioni. Bug = posizioni tenute troppo a lungo o chiuse prematuramente.

Test da scrivere:
- PnL +8% su bond -> TAKE_PROFIT
- PnL -3% su bond -> STOP_LOSS
- Trade bond vecchio 15gg (>336h) -> TIME_EXIT
- PnL +5% su bond (sotto TP) -> HOLD
- Strategia sconosciuta -> usa DEFAULT_BARRIER

---

### P1 — Importanti (logica di monitoraggio e conversione)

#### 7. `SignalConverter` — adattatori strategia -> UnifiedSignal
Test da scrivere:
- `from_bond_opportunity()`: side sempre "YES", confidence = certainty_score
- `from_event_opportunity()`: side normalizzato a upper case
- `from_whale_opportunity()`: kelly_size = copy_size del whale
- `from_weather_opportunity()`: category sempre "weather"
- `_days_until()` con data futura, passata, invalida, vuota

#### 8. `AttributionEngine` — Brier score e alpha decay
Test da scrivere:
- `record_entry()` + `record_exit()` con win -> brier_score = (prob - 1)^2
- `record_exit()` con loss -> brier_score = (prob - 0)^2
- `get_brier_score()` senza dati -> 0.25 (random)
- `get_alpha_decay()` con < 10 campioni -> 1.0
- `get_alpha_decay()` con win rate in calo -> < 1.0
- Rolling window: > 5000 completati -> troncato

#### 9. `DriftDetector` — concept drift
Test da scrivere:
- Win rate calo > 30% -> alert "concept_drift" con severity HIGH/MEDIUM
- Win rate stabile -> nessun alert
- Meno di 20 campioni -> nessun alert (dati insufficienti)
- Spread raddoppiato -> alert "microstructure"
- `get_strategy_health()` senza dati -> status "NO_DATA"
- `get_strategy_health()` con drift -> status "DRIFTING"

#### 10. `CalibrationEngine` — suggerimenti parametri
Test da scrivere:
- Brier > 0.35 -> suggerisce aumento min_edge
- Brier < 0.15 -> suggerisce riduzione min_edge
- Alpha < 0.50 -> suggerisce dimezzamento kelly_fraction
- Alpha > 1.30 -> suggerisce aumento kelly +20%
- Nessuna attribution -> lista suggerimenti vuota

#### 11. `CorrelationMonitor` — limite esposizione per tema
Test da scrivere:
- `classify_theme()` con "bitcoin price prediction" -> "crypto"
- `classify_theme()` con "Trump election" -> "politics"
- `classify_theme()` con testo senza keyword -> "other"
- `check_correlation()` sotto il limite 40% -> allowed
- `check_correlation()` sopra il limite 40% -> blocked
- Senza risk_manager -> sempre allowed

#### 12. `TailRiskAgent` — VaR e worst-case
Test da scrivere:
- Nessuna posizione aperta -> risk_level NORMAL, max_loss = 0
- Esposizione > 50% capitale -> risk_level CRITICAL
- Posizione > 10% capitale -> in concentrated_positions
- VaR 95% calcolato correttamente (formula binomiale)

#### 13. `Config.validate()` — validazione configurazione
Test da scrivere:
- Allocazione che somma a 100 -> nessun errore
- Allocazione != 100 -> errore
- API key mancante -> errore
- `capital_for("bond")` con allocazione 30% e capitale $1000 -> $300

---

### P2 — Utili (logica di orchestrazione ed esecuzione)

#### 14. `ExecutionAgent` — TWAP vs LIMIT_MAKER
Test da scrivere:
- Trade $20 -> plan con strategy LIMIT_MAKER, splits=1
- Trade $50 -> plan con strategy TWAP, splits>=2
- `tranche_size` = total_size / splits
- Paper trading: sempre full fill con slippage casuale

#### 15. `OrchestratorAgent` — prioritizzazione mercati
Test da scrivere:
- Volume < 100 -> SKIP
- Volume spike > 3x media -> CRITICAL
- Prezzo YES > 0.93 -> HIGH
- Volume > 50K e spread < 2% -> MEDIUM
- Routing: CRITICAL -> tutte le strategie, LOW -> solo data_driven
- `_anomaly_score()` con volume spike -> score > 0

---

### P3 — Nice-to-have (feed esterni, storage)

#### 16. Feed esterni (GDELT, Finlight, Binance WS)
Test da scrivere:
- Circuit breaker: dopo N errori -> blocco con cooldown
- GDELT: query semplici (1 per categoria), intervallo 10s
- Finlight: backoff esponenziale su 429
- Binance: backoff con jitter, stale detection (> 60s)
- Mock delle API HTTP per test offline

#### 17. Storage (database.py, redis_bus.py)
Test da scrivere:
- Graceful degradation: se PG/Redis non disponibili -> fallback senza crash
- EventBus fallback in-memory
- Serializzazione/deserializzazione trade

---

## Piano di implementazione raccomandato

### Struttura proposta
```
tests/
  conftest.py           # Fixture condivise (RiskConfig, mock market, mock signal)
  test_signal_validator.py    # P0
  test_devils_advocate.py     # P0
  test_risk_manager.py        # P0 (can_trade + kelly_size + check_barrier)
  test_vpin_monitor.py        # P0
  test_signal_converter.py    # P1
  test_attribution.py         # P1
  test_drift_detector.py      # P1
  test_calibration.py         # P1
  test_correlation_monitor.py # P1
  test_tail_risk.py           # P1
  test_config.py              # P1
  test_execution_agent.py     # P2
  test_orchestrator.py        # P2
```

### Fixture principali per `conftest.py`
```python
@pytest.fixture
def risk_config():
    return RiskConfig(total_capital=1000.0, max_bet_size=40.0, ...)

@pytest.fixture
def risk_manager(risk_config):
    return RiskManager(risk_config)

@pytest.fixture
def sample_signal():
    return UnifiedSignal(
        strategy="event_driven", market_id="mkt_001",
        question="Will X happen?", side="YES", price=0.60,
        edge=0.05, confidence=0.75, signal_type="news_reactive",
        volume=10000, liquidity=5000, spread=0.02,
    )
```

### Ordine di implementazione
1. **`conftest.py`** + **`test_signal_validator.py`** + **`test_devils_advocate.py`** — coprono il Layer 2 (gate finale)
2. **`test_risk_manager.py`** — copre il componente piu' grande e critico (722 LOC, 12+ checks)
3. **`test_vpin_monitor.py`** — copre toxic flow detection (algoritmo matematico puro, facile da testare)
4. **`test_config.py`** — validazione configurazione (prevenzione errori di deploy)
5. **Monitoring:** `test_attribution.py`, `test_drift_detector.py`, `test_calibration.py`
6. **Risk Layer 3:** `test_correlation_monitor.py`, `test_tail_risk.py`
7. **Execution/Orchestration:** `test_execution_agent.py`, `test_orchestrator.py`

### Dipendenze
- `pytest` (non presente nel progetto, da aggiungere)
- `pytest-asyncio` per `ExecutionAgent` e `OrchestratorAgent` (metodi async)
- Nessuna dipendenza esterna necessaria per i test P0/P1 (tutti unit test puri con mock)
