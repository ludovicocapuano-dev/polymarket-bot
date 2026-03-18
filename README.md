# Polymarket Multi-Strategy Trading Bot

Automated prediction market trading bot for [Polymarket](https://polymarket.com) with multi-strategy architecture, AI-powered crowd simulation, and self-evolving parameter optimization.

## Performance

| Metric | Value |
|--------|-------|
| Weather strategy (best) | +$2,323 PnL, 63.4% WR, 325 trades |
| Golden era (Mar 3-14) | +$1,908, 72% WR, $159/day |
| Perplexity API savings | $57+ via local Ollama LLM |
| Strategies | 10 built, 3 active (focus > diversification) |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR                          │
│  Market scanning → Signal validation → Risk management   │
├─────────────┬───────────────┬───────────────────────────┤
│  STRATEGIES │   AI LAYER    │      DATA PIPELINE        │
│             │               │                           │
│  Weather    │  4096-Agent   │  REST API (Gamma/CLOB)    │
│  Sniper     │  Crowd Sim    │  WebSocket (real-time)    │
│  Econ Data  │  Deep Research│  SQLite WAL (storage)     │
│  Crowd Sport│  Bayesian     │  Market lifecycle         │
│             │  Updating     │  VWAP / Flow analysis     │
├─────────────┴───────────────┴───────────────────────────┤
│                   RISK MANAGEMENT                        │
│  Quarter Kelly · Triple Barrier · EV Gate · Base Rates   │
├─────────────────────────────────────────────────────────┤
│                 SELF-EVOLVING OPTIMIZER                   │
│  Genetic Programming · Meta-Optimizer · Auto-Compound    │
└─────────────────────────────────────────────────────────┘
```

## Strategies

### Active
| Strategy | Description | Edge Source |
|----------|-------------|------------|
| **Weather** | BUY_NO on temperature range markets | Multi-source forecast divergence (WU + OpenMeteo) |
| **Resolution Sniper** | Near risk-free resolution sniping | UMA oracle + local LLM verification |
| **Econ Data Sniper** | Trade on BLS data releases within ~1s | Polls BLS page at 100ms, parses headline number |
| **Crowd Sport** | 50-agent Delphi simulation on sport markets | 10 specialist groups, 3-round consensus |
| **Crowd Prediction** | 4096-agent hierarchical simulation on politics/crypto/geopolitics/entertainment | 64 groups → 16 panels → 4 summits → consensus |

### Key Design Decisions (from 112K wallet study)
- **Specialize**: Focus on 1-2 categories (+$4,200 avg) instead of 5+ (-$2,100 avg)
- **Quarter Kelly**: Never risk more than 25% of Kelly optimal
- **Price-based exits**: Sell on price movement (18-72h hold), don't wait for resolution
- **Min edge 8%**: Top 1% enter at 8-10% deviation from consensus
- **Never negative EV**: Strict EV ≥ 0.10 gate on all entries

## Core Formulas

```python
# 1. Expected Value — decides every entry
EV = win_prob × payoff_ratio - (1 - win_prob)  # must be ≥ 0.10

# 2. Bayes — chains evidence updates
P(H|E) = P(E|H) · P(H) / P(E)

# 3. Kelly — position sizing
f* = (p · b − q) / b  × 0.25  # quarter Kelly

# 4. Base Rate — the invisible edge
edge = base_rate - market_price  # trade when |edge| > 8%

# 5. KL-Divergence — arbitrage scanner
D_KL(P‖Q) = Σ Pᵢ · ln(Pᵢ / Qᵢ)  # finds mispriced correlated markets
```

## Tech Stack

### Trading
- **Polymarket CLOB** — order execution (Builder Program, gasless)
- **Horizon SDK** — TWAP/VWAP/Iceberg for large orders
- **PMXT** — cross-platform scanning (Polymarket + Kalshi)

### AI / ML
- **DeepSeek V3** — crowd simulations via LiteLLM proxy (~$0.001/call)
- **Ollama** (qwen2.5:0.5b) — local LLM for resolution verification
- **all-minilm** — market embeddings for correlation clustering
- **TSFresh** — automatic feature extraction for meta-labeler
- **Prophet** — PnL forecasting with weekly seasonality
- **ARCH** — GARCH/EGARCH/GJR volatility modeling

### Data
- **Unusual Whales** — congress trades, dark pool, insider signals, crypto whales
- **FRED** — economic data (NFP, unemployment, CPI consensus)
- **ESPN** — sport statistics for crowd simulations
- **SQLite WAL** — structured storage with idempotent writes

### Risk
- **Riskfolio-lib** — CVaR/MVO/HRP portfolio optimization
- **VectorBt** — vectorized backtesting (5-10x faster optimizer)
- **PyFolio** — tearsheet analytics (Sharpe, Sortino, drawdown)

## Self-Evolving System

The bot evolves its own optimization function:

```
AutoOptimizer v4.0 — Self-Evolving Scoring Genome
┌────────────────────────────────────────────────┐
│ ScoringGenome (15 mutable coefficients)        │
│   wr_base, wr_scale, wr_center, wr_range       │
│   pf_base, pf_scale, pf_center, pf_range       │
│   vol_norm, vol_exp                             │
│   pnl_exp, wr_exp, pf_exp                      │
│   simplicity_floor, drift_tolerance             │
├────────────────────────────────────────────────┤
│ Meta-Optimizer (genetic programming)           │
│   Population: 12 genome variants               │
│   Selection: test PnL (not genome score)       │
│   Generations: 5 per run, 2x/day               │
│   Result: genome that produces best REAL params │
└────────────────────────────────────────────────┘
```

## Setup

```bash
# Clone
git clone https://github.com/ludovicocapuano-dev/polymarket-bot.git
cd polymarket-bot

# Configure
cp .env.example .env
# Edit .env with your API keys

# Run (paper trading)
python3 bot.py

# Run (live — requires confirmation)
echo 'CONFERMO' | python3 bot.py --live
```

### Required API Keys
- Polymarket CLOB (private key + API credentials)
- DeepSeek (for crowd simulations)
- Unusual Whales (for signal intelligence)

### Optional
- Anthropic (for premium simulations)
- Horizon SDK (for advanced execution)
- FRED (for economic data)
- Zep Cloud (for MiroFish memory)

## Cron Jobs

| Schedule | Task |
|----------|------|
| 4x/day (00:23, 06:23, 12:23, 18:23) | AutoOptimizer + meta-evolve |
| 2x/day (08:00, 20:00) | GitHub repo monitoring (8 repos) |
| 1x/day (04:00) | AutoContext bridge |

## Monitored Repositories

- karpathy/autoresearch
- hyperspaceai/agi
- greyhaven-ai/autocontext
- 666ghj/MiroFish
- MiroMindAI/MiroThinker
- polymarket/polymarket-cli
- polymarket/py-clob-client
- polymarket/py-builder-signing-sdk

## Key Lessons Learned

1. **Reserve floor must scale with real capital** — caused trading stall when floor > USDC
2. **Weather works with golden-era params** (5-8% min edge) — don't over-tighten
3. **BLS blocks bots** without browser User-Agent — need Chrome headers
4. **Favorite-longshot bias doesn't work** on Polymarket (alpha too low, 0% WR)
5. **MiroFish full simulation is too slow** for trading — Delphi hierarchical is 1000x faster
6. **112K wallet study**: specialize, quarter Kelly, price exits, never negative EV
7. **75% of Polymarket markets resolve NO** — structural tailwind for BUY_NO strategies

## License

Private repository. All rights reserved.

---

*Built with [Claude Code](https://claude.ai/claude-code)*
