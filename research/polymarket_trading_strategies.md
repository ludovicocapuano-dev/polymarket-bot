# Polymarket Trading Strategies: Comprehensive Research Report
## Compiled March 2026 from Academic Papers, On-Chain Data, and Practitioner Sources

---

## TABLE OF CONTENTS
1. [Executive Summary](#executive-summary)
2. [Strategy 1: Weather Market Forecasting Edge](#strategy-1-weather-market-forecasting-edge)
3. [Strategy 2: High-Probability Bonds (Endgame Sweep)](#strategy-2-high-probability-bonds)
4. [Strategy 3: Market Making / Liquidity Provision](#strategy-3-market-making)
5. [Strategy 4: Information Arbitrage (Speed Trading)](#strategy-4-information-arbitrage)
6. [Strategy 5: Cross-Platform Arbitrage](#strategy-5-cross-platform-arbitrage)
7. [Strategy 6: Intra-Market Arbitrage (Complete Set)](#strategy-6-intra-market-arbitrage)
8. [Strategy 7: Domain Specialization](#strategy-7-domain-specialization)
9. [Strategy 8: Whale Copy Trading](#strategy-8-whale-copy-trading)
10. [Strategy 9: Longshot Bias Exploitation](#strategy-9-longshot-bias-exploitation)
11. [Strategy 10: Resolution Timing Strategies](#strategy-10-resolution-timing)
12. [Kelly Criterion for Prediction Markets](#kelly-criterion)
13. [Market Microstructure Insights](#market-microstructure)
14. [Key Statistics and Reality Check](#reality-check)
15. [Open Source Tools and Repos](#tools-and-repos)
16. [Sources](#sources)

---

## Executive Summary <a name="executive-summary"></a>

Research across academic papers, on-chain data analysis, and practitioner reports reveals that
**only 7.6-16.8% of Polymarket wallets are profitable**, and only 0.51% achieved profits
exceeding $1,000. However, the profitable minority employs systematic strategies with
mathematical edges. The six most proven strategies, ranked by viability in 2025-2026:

| Strategy | Expected Edge | Capital Req. | Risk | Still Viable 2026? |
|----------|--------------|-------------|------|-------------------|
| Weather Forecast Arb | 15-40% per trade | $100-$5,000 | Low-Medium | **YES - Best for small capital** |
| High-Prob Bonds | 2-5% per trade (1800% ann.) | $10,000+ | Low (black swan tail) | **YES** |
| Market Making | 80-200% ann. (new mkts) | $10,000-$100,000 | Medium | **YES but declining** |
| Domain Specialization | Varies (96% win rate possible) | $1,000-$50,000 | Medium | **YES - Evergreen** |
| Information/Speed Arb | High but shrinking | $10,000+ + infra | High | **Declining rapidly** |
| Cross-Platform Arb | 1-7.5% per trade | $5,000+ | Low | **Mostly automated away** |

---

## Strategy 1: Weather Market Forecasting Edge <a name="strategy-1-weather-market-forecasting-edge"></a>

### Overview
The single most accessible and proven strategy for retail traders in 2025-2026. Exploits the
gap between professional meteorological forecasts (NOAA, NWS) and Polymarket crowd-sourced odds.

### How It Works
1. Pull real-time forecast data from NOAA/NWS APIs
2. Convert forecasts to probability distributions across temperature buckets
3. Compare forecast probabilities to Polymarket bucket prices
4. Buy YES shares when forecast probability >> market implied probability
5. Buy NO shares when forecast probability << market implied probability

### Mathematical Basis
- NOAA temperature forecasts have well-documented accuracy metrics
- 24-hour forecasts: ~90% accuracy within 3 degrees F
- 48-hour forecasts: ~80% accuracy within 3 degrees F
- Market participants are often "just guessing" vs. actual meteorological science
- Edge = |NOAA_probability - Market_implied_probability|

### Specific Trading Rules (from successful bots)
- **Entry**: Buy when market price < $0.15 for a bucket NOAA says has >30% chance
- **Exit**: Sell when price corrects above $0.45 (3x minimum return) or at resolution
- **Position size**: $1-$2 per position for conservative approach
- **Markets**: NYC, London, Seoul temperature markets are most liquid

### Proven Track Records
- **gopfan2**: $2M+ net profit, primarily from weather markets
- One trader: $1.11M profit from a single $92K bet on London weather (8% implied, hit)
- Bot operators: $1,000 -> $24,000 scaling (London weather markets since April 2025)
- Another bot: $65,000 profits across NYC, London, Seoul

### Expected Edge / Win Rate
- 15-40% edge per mispriced trade when forecast diverges significantly from market
- Win rate depends on forecast accuracy and edge threshold selected
- Higher thresholds (larger divergence required) = higher win rate but fewer trades

### Capital Requirements
- Minimum: $100
- Recommended: $1,000-$5,000
- Can scale significantly with automation

### Risk Profile
- **Low-Medium**: Capped downside per position ($1-$2 per trade in conservative mode)
- Risk of weather forecast errors (rare but possible for extreme events)
- Risk of market manipulation on thin-liquidity weather markets
- Laddering across multiple temperature buckets reduces concentration risk

### Implementation
- **Manual**: Check NOAA forecasts, compare to Polymarket prices, trade manually
- **Automated**: Use simmer-weather SDK, OpenClaw framework, or custom Python bot
- **Data source**: NOAA API (api.weather.gov), NWS forecast endpoints
- **Key Python packages**: polymarket CLOB client, requests for NOAA API

### Viability in 2026
**HIGH** - Weather markets continue to attract unsophisticated participants. NOAA data is
free and accurate. Competition is increasing but markets remain inefficient enough for
meaningful edge. Best strategy for small capital retail traders.

---

## Strategy 2: High-Probability Bonds (Endgame Sweep) <a name="strategy-2-high-probability-bonds"></a>

### Overview
Buy near-certain outcomes (contracts priced $0.95-$0.99) and hold to resolution. Treat
prediction markets as short-term, high-yield fixed-income products denominated in USDC.

### How It Works
1. Identify events where the outcome is virtually certain (bill passed, game ended, etc.)
2. Buy YES shares at $0.95-$0.99 before official market resolution
3. Collect $1.00 per share at resolution
4. Profit = $1.00 - purchase_price per share

### Mathematical Basis
- Return per trade: (1.00 - price) / price
- At $0.95: 5.26% return per trade
- At $0.97: 3.09% return per trade
- At $0.99: 1.01% return per trade
- Resolution time: typically 24-72 hours
- **Annualized equivalent at 5.2% per 72-hour cycle: ~1,800% APR with compounding**

### Key Statistics
- 90% of large orders ($10K+) on Polymarket occur at price levels above $0.95
- Top practitioners earn $150,000+ annually from a few weekly trades

### Risk Profile
- **LOW** with **catastrophic tail risk** (black swan events)
- Core risk: events that seem certain but resolve unexpectedly
- A single loss can wipe profits from dozens of successful trades
- Must identify "pseudo-certainties" vs. genuine near-certain outcomes

### Risk Management Rules
- Never allocate more than 10% of capital to a single bond position
- Focus on events with verifiable external confirmation (official results published)
- Avoid events dependent on subjective interpretation or appeals
- Maintain a portfolio approach - spread across many concurrent positions

### Capital Requirements
- Minimum effective: $10,000 (to generate meaningful absolute returns)
- Recommended: $50,000+
- Returns scale linearly with capital

### Viability in 2026
**HIGH** - Fundamentally driven by the time delay between outcome certainty and market
resolution. Polymarket's UMA oracle challenge period (2 hours) creates a structural window.
Competition is increasing but capacity remains large.

### Additional Yield
- Polymarket offers 4% annualized daily rewards on qualifying long-term positions
- Stacks on top of bond strategy returns

---

## Strategy 3: Market Making / Liquidity Provision <a name="strategy-3-market-making"></a>

### Overview
Provide liquidity on both sides of prediction markets, earning the bid-ask spread. Neutral
strategy that profits from trading activity rather than outcome prediction.

### How It Works
1. Place buy orders below current mid-price (e.g., bid YES at $0.48)
2. Place sell orders above current mid-price (e.g., offer YES at $0.52)
3. When both sides fill, earn the spread ($0.04 in this example)
4. Manage inventory to avoid directional exposure at settlement

### Mathematical Basis: Avellaneda-Stoikov Model (Adapted)

The optimal reservation price and spread for a market maker:

```
Reservation price: r(s,q,t) = s - q * gamma * sigma^2 * (T - t)

Where:
  s = current mid-price
  q = current inventory position
  gamma = risk aversion parameter
  sigma = price volatility
  T = time to settlement
  t = current time

Optimal spread: delta = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/k)

Where:
  k = order arrival rate parameter
```

**Prediction market adaptations:**
- Binary settlement (0 or 1) creates asymmetric inventory risk
- Event-driven price jumps require wider spreads around news catalysts
- Position merging: when holding both YES and NO, merge to recover USDC

### Expected Returns
| Capital Level | Monthly Potential | Annualized |
|---------------|------------------|------------|
| $1,000-$5,000 | $50-$200 | 12-48% |
| $5,000-$25,000 | $200-$1,000 | 12-48% |
| $25,000-$100,000 | $1,000-$5,000 | 12-60% |
| $100,000+ | $5,000-$20,000+ | 60-200%+ |

**New/illiquid markets**: 80-200% annualized equivalent
**Peak documented**: One trader earned $700-800 daily on $10K starting capital

### Inventory Management Techniques
1. **Quote skewing**: Adjust mid-prices to encourage trades reducing unwanted positions
2. **Position limits**: Set maximum thresholds per market
3. **Dynamic hedging**: Offset inventory across correlated markets
4. **Time-based reduction**: Aggressively close positions as settlement approaches
5. **Position merging**: When holding YES + NO, merge to USDC (no slippage)

### Risk Controls
- **Event risk**: News moves markets 40-50 points instantly; use news monitoring + circuit breakers
- **Liquidity risk**: Thin markets move dramatically on small orders
- **Technical risk**: System failures are catastrophic; use dedicated VPS infrastructure
- **Adverse selection**: Informed traders systematically pick off stale quotes

### Infrastructure Requirements
- **Latency target**: Sub-10ms total round-trip
- **Server**: 4+ cores, 8-16GB RAM, 100GB+ NVMe SSD
- **Location**: US East Coast (New York optimal) for Polymarket
- **Network**: 1Gbps with low-latency routing
- **Monitoring**: Real-time order book WebSocket feeds (<50ms updates)

### Capital Requirements
- Minimum: $5,000
- Recommended: $25,000+
- Must be prepared to have capital locked across multiple markets

### Polymarket-Specific Features
- **Post-only orders** (since Jan 2026): Limit orders rejected if they'd immediately match
- **Maker rebates**: Daily USDC rebates funded by taker fees for liquidity providers
- **Liquidity rewards**: Additional rewards on select markets

### Viability in 2026
**MODERATE-HIGH** - Still viable but increasingly competitive. Returns and opportunities
are shrinking post-election cycle. Best edge in new/illiquid markets. Professional market
makers with infrastructure advantages dominate liquid markets.

---

## Strategy 4: Information Arbitrage / Speed Trading <a name="strategy-4-information-arbitrage"></a>

### Overview
Exploit the lag between real-world information release and Polymarket price adjustment.
When breaking news occurs, prices often lag 30 seconds to several minutes.

### How It Works
1. Monitor real-time information feeds (news wires, official data releases, social media)
2. When material information hits, immediately calculate new fair probability
3. Execute trades before market adjusts to new information
4. Profit from the information-to-price-digestion window

### Historical Examples
- Federal Reserve Powell statement: 8-second price jump from $0.65 to $0.78
- French trader Theo: Spent <$100K on specialized polling -> $85M profit
- Top algorithmic traders: 10,200+ speed trades generating $4.2M profit (2024-2025)

### Mathematical Basis
- Edge = P(true outcome | new info) - P(current market price)
- Information decay: Edge shrinks exponentially as market absorbs news
- Kyle's lambda model: Price impact coefficient declined from 0.518 to 0.01 as markets matured
- Early-stage: $1M order moved prices ~13 percentage points
- Mature market: $1M order moves prices ~0.25 percentage points

### Speed Requirements
- Arbitrage window compressed from minutes (2024) to seconds (2026)
- Average opportunity duration: 2.7 seconds (down from 12.3s in 2024)
- 73% of arbitrage profits captured by sub-100ms execution bots

### Capital Requirements
- Trading capital: $10,000+
- Infrastructure: Dedicated servers, news API subscriptions
- Development: Custom low-latency trading systems

### Risk Profile
- **HIGH**: Requires speed, accuracy, and significant infrastructure investment
- Wrong information interpretation leads to immediate losses
- Arms race with other speed traders continuously erodes edge
- Technical failures during fast-moving events are costly

### Viability in 2026
**DECLINING** - Alpha rapidly disappearing as competition increases. Not recommended for
retail traders without significant technical expertise and infrastructure. Still viable for
those with genuine information advantages (domain expertise + speed).

---

## Strategy 5: Cross-Platform Arbitrage <a name="strategy-5-cross-platform-arbitrage"></a>

### Overview
Exploit price differences for identical events across Polymarket, Kalshi, PredictIt,
and sportsbooks by buying low on one platform and selling high on another.

### How It Works
1. Monitor the same event across multiple platforms simultaneously
2. When price divergence exceeds transaction costs, execute opposing positions
3. Buy YES on platform A (cheaper), buy NO on platform B (cheaper)
4. Guaranteed profit regardless of outcome = (1 - YES_A - NO_B) - fees

### Documented Results
- Over $40 million extracted from Polymarket April 2024 - April 2025
- Top 3 wallets earned $4.2M combined
- Example returns: 7.5% risk-free profit within one hour

### Key Risks
- **Settlement mismatch**: Different platforms may interpret resolution criteria differently
- **Execution risk**: Prices move before both legs are filled
- **Capital lockup**: Funds locked on both platforms until resolution
- **Platform risk**: Withdrawal delays, account restrictions

### Capital Requirements
- Minimum: $5,000 (accounts on multiple platforms)
- Recommended: $25,000+ for meaningful returns
- Requires accounts and capital across 2+ platforms

### Viability in 2026
**LOW for manual traders** - Bots close gaps in milliseconds. Bid-ask spreads compressed
from 4.5% (2023) to 1.2% (2025). Manual execution is essentially impossible. Only viable
with automated systems, and even then competition is fierce.

---

## Strategy 6: Intra-Market Arbitrage (Complete Set) <a name="strategy-6-intra-market-arbitrage"></a>

### Overview
Exploit mispricing within a single market when YES + NO prices don't sum to $1.00,
or when multi-outcome market prices don't sum correctly.

### Types
1. **Buy-All Arbitrage**: When sum of all outcomes < $1.00, buy all contracts
2. **Sell-All Arbitrage**: When sum of all outcomes > $1.00, sell all contracts
3. **Logical inconsistency**: Related markets with contradictory implied probabilities

### Mathematical Basis
- For binary market: If YES + NO < $1.00, buy both for risk-free profit
- For multi-outcome: If sum of all outcomes < $1.00, buy the complete set
- Profit = $1.00 - total_cost_of_complete_set

### Historical Data
- PredictIt during 2016 elections: Arbitrage profits up to 55% per contract
- Polymarket 2024-2025: Spreads much tighter, typically 1-3%

### Viability in 2026
**LOW** - Automated bots detect and close these gaps within seconds. Average opportunity
duration is 2.7 seconds. Only viable with automated, low-latency systems.

---

## Strategy 7: Domain Specialization <a name="strategy-7-domain-specialization"></a>

### Overview
Develop deep expertise in a narrow field (sports, politics, crypto, etc.) and trade only
markets where you have a genuine information or analytical advantage.

### How It Works
1. Choose 1-3 domains where you have or can build genuine expertise
2. Develop proprietary models, data sources, or analytical frameworks
3. Only trade when your model identifies significant mispricing
4. Make 10-30 high-conviction trades per year

### Proven Track Records
- **HyperLiquid0xb**: $1.4M total profit; $755K single trade on baseball prediction
- **Axios**: 96% accuracy on "mention markets" through extensive statement analysis
- **Theo (French trader)**: $85M from election markets using proprietary polling

### Mathematical Basis
- Edge = |Your_probability_estimate - Market_probability|
- Size positions using Kelly Criterion (see section below)
- Requires calibrated probability estimates (track record of accuracy)

### Capital Requirements
- Minimum: $1,000
- Recommended: $10,000+
- More important: Time investment (10,000+ hours of specialization)

### Risk Profile
- **MEDIUM**: High conviction means fewer diversification opportunities
- Risk of overconfidence in your own expertise
- Small number of trades means variance can be high

### Viability in 2026
**HIGH - Evergreen strategy**. Human expertise in niche domains is difficult to replicate
or automate. The more obscure the domain, the less competition. This is the strategy most
consistently cited by top Polymarket traders.

---

## Strategy 8: Whale Copy Trading <a name="strategy-8-whale-copy-trading"></a>

### Overview
Track profitable wallets on-chain and replicate their trades with a time delay.

### How It Works
1. Identify consistently profitable wallets using on-chain analytics
2. Set up real-time alerts for new positions from tracked wallets
3. Execute copy trades within seconds of seeing the original
4. Apply position sizing based on your own risk management rules

### Wallet Selection Criteria
- Positive all-time P&L
- Consistent recent performance (30-day and 7-day P&L positive)
- Win rate above 55%
- At least 50+ closed positions for statistical significance
- Specialization in 2-3 topic areas (not random betting)

### Tools
- Polymarket Analytics (polymarketanalytics.com) - trader leaderboard
- Polywhaler (polywhaler.com) - whale tracker
- PolyTrack (polytrackhq.app) - copy trading tools
- Dune Analytics dashboards for on-chain data
- Custom blockchain event monitoring for real-time alerts

### Key Limitations
- **Slippage**: If price moves >10% since whale's entry, skip the trade
- **Wash trading**: ~15% of wallets show activity consistent with wash trading
- **Incomplete picture**: You see one leg of a trade, not the full strategy
- **Fragility**: Relying on one trader introduces single-point-of-failure risk

### Capital Requirements
- Minimum: $1,000
- Recommended: $5,000+
- Need fast execution infrastructure for time-sensitive copy trading

### Viability in 2026
**MODERATE** - Viable as supplementary signal but not as primary strategy. Best used to
validate your own analysis rather than blind copying. The most sophisticated whales may
use multi-wallet strategies that obscure their true positions.

---

## Strategy 9: Longshot Bias Exploitation <a name="strategy-9-longshot-bias-exploitation"></a>

### Overview
Systematic bias where participants overvalue longshots (low-probability events) and
undervalue favorites (high-probability events). Betting on favorites provides structural edge.

### Academic Evidence
- Football betting: Average returns of -3.64% on favorites vs. -26.08% on underdogs
- Kalshi analysis: Low-price contracts win far less often than required to break even
- High-price contracts win more often and yield small positive returns
- Well-documented in academic literature across multiple markets and decades

### Trading Implementation
- Systematically buy contracts priced $0.70-$0.90 where fundamentals support favorite
- Avoid low-probability contracts ($0.05-$0.20) unless you have genuine information edge
- The bias is strongest in markets with many retail participants
- Most exploitable in markets with clear "underdog narrative" appeal

### Mathematical Basis
- Behavioral: People are systematically poor at distinguishing small from tiny probabilities
- Risk-seeking in losses (prospect theory) drives overvaluation of longshots
- Limited arbitrage mechanisms allow the bias to persist

### Expected Edge
- Small but systematic: 2-5% edge per trade on average
- Requires large volume of trades for edge to manifest
- Best combined with Kelly Criterion position sizing

### Viability in 2026
**MODERATE** - The bias persists because it's rooted in human psychology, not market
structure. However, as algorithmic traders increase, the bias may shrink. Still exploitable
in markets dominated by retail participants.

---

## Strategy 10: Resolution Timing Strategies <a name="strategy-10-resolution-timing"></a>

### Overview
Exploit the mechanics of Polymarket's resolution process, including the UMA oracle
challenge period and timing ambiguities.

### Resolution Process
1. Event concludes
2. User proposes outcome + posts $750 bond
3. 2-hour challenge period begins
4. If no challenge: outcome accepted, market closes, bond returned + reward
5. If challenged: escalates to UMA Dispute Resolution process

### Trading Opportunities
- **Pre-resolution accumulation**: Buy near-certain outcomes before formal proposal
- **Challenge period trading**: Markets can still trade during 2-hour challenge window
- **Resolution ambiguity**: When resolution criteria are subjective, clarification requests
  can create volatility and trading opportunities
- **Cross-resolution arb**: When multiple related markets resolve at different times

### Risks
- Incorrect resolution proposals lose $750 bond
- Disputed resolutions can take days/weeks, locking capital
- Strategic clarification requests by other traders can manipulate prices

### Viability in 2026
**MODERATE** - Structural opportunity created by oracle mechanics. Low competition because
it requires deep understanding of Polymarket's resolution infrastructure.

---

## Kelly Criterion for Prediction Markets <a name="kelly-criterion"></a>

### The Core Formula

For binary prediction market contracts:

```
f* = (Q - P) / (1 + Q)

Where:
  f* = optimal fraction of bankroll to bet
  Q  = q / (1 - q)    (odds ratio of your probability estimate)
  P  = p / (1 - p)    (odds ratio of market price)
  q  = your estimated probability
  p  = market price (implied probability)
```

### Simplified Kelly for Prediction Markets

For a YES contract at price p, where you believe true probability is q:

```
f* = (q - p) / (1 - p)    [when betting YES, q > p]
f* = (p - q) / p           [when betting NO, q < p]
```

### Practical Adjustments

**Fractional Kelly**: Most practitioners use 25-50% of full Kelly to:
- Reduce drawdown risk (full Kelly has ~50% peak-to-trough drawdowns)
- Account for estimation error in probability assessment
- Smooth equity curve and reduce emotional stress

**Sensitivity Analysis (from academic paper arXiv:2412.14144v1)**:
- Bet-sizing mistakes are penalized **quadratically** (second-order effect)
- Probability estimation errors are penalized **linearly** (first-order effect)
- Implication: Getting your probability estimate roughly right matters more than
  perfect bet sizing, but oversizing bets is more dangerous than slight misprobability

### Multi-Outcome Kelly

For markets with N mutually exclusive outcomes:

```
Maximize: sum_i [ q_i * log(1 + f_i * (1/p_i - 1)) ]
Subject to: sum_i f_i <= 1, f_i >= 0
```

### Practical Implementation Rules

1. **Never bet more than Kelly** - Overbetting reduces long-run growth rate
2. **Use fractional Kelly (0.25-0.5x)** - Accounts for estimation uncertainty
3. **Track your calibration** - If you say 70%, it should happen 70% of the time
4. **Update continuously** - Recalculate Kelly fraction as market prices change
5. **Consider correlation** - Reduce total exposure when positions are correlated

### Example Calculation

- Market price (YES): $0.40
- Your estimate: 60% probability
- Full Kelly: f* = (0.60 - 0.40) / (1 - 0.40) = 0.333 (33.3% of bankroll)
- Half Kelly: 0.167 (16.7% of bankroll)
- Quarter Kelly: 0.083 (8.3% of bankroll)

---

## Market Microstructure Insights <a name="market-microstructure"></a>

### Polymarket CLOB Architecture
- **Hybrid-decentralized**: Off-chain matching, on-chain settlement (Polygon)
- **Order types**: Limit orders, post-only orders (since Jan 2026)
- **Fee structure**: Taker fees fund maker rebates
- **Mirrored order books**: Every event has unified YES/NO books

### Key Findings from Academic Research (arXiv:2603.03136)

**Price Impact (Kyle's Lambda)**:
- Early markets: lambda ~0.518 ($1M moves price 13 percentage points)
- Mature markets: lambda ~0.01 ($1M moves price 0.25 percentage points)
- 50x improvement as liquidity deepened

**Trading Patterns**:
- 71.8% of traders traded Trump YES in 2024 election
- Only 0.7% traded across more than 2 candidate markets
- Pronounced intraday seasonality aligned with U.S. business hours
- Top 10% traders concentrated during American market hours

**Market Efficiency**:
- YES + NO deviations from $1.00 narrowed as volume increased
- Arbitrage deviations compressed over time
- Market demonstrated resilience absorbing $30M+ single-actor positions

### Order Flow Analysis for Edge
- Adverse selection (being picked off by informed traders) is primary cost
- Speed of cancellation when news breaks determines profitability for makers
- Post-only orders + rebate structure favors patient liquidity providers
- Market impact is highly asymmetric between liquid and illiquid markets

---

## Key Statistics and Reality Check <a name="reality-check"></a>

### Sobering Numbers
- **80%** of Polymarket participants lose money over time
- **Only 7.6-16.8%** of wallets show net gain
- **Only 0.51%** of wallets achieved profits exceeding $1,000
- **~15%** of wallets show activity consistent with wash trading
- Bid-ask spreads compressed from **4.5% (2023) to 1.2% (2025)**
- Average arbitrage window: **2.7 seconds** (down from 12.3s in 2024)

### Three Traits of Winning Traders
1. **Systematically capture market pricing errors** (not lucky guesses)
2. **Obsessive risk management** (position sizing, diversification, stop rules)
3. **Information advantage in specific domains** (not trying to trade everything)

### What Doesn't Work
- Blind contrarian betting
- Following social media "tips" without independent analysis
- Manual arbitrage (too slow in 2026)
- Overtrading across too many markets without expertise
- Full Kelly sizing without calibrated probability estimates

---

## Open Source Tools and Repos <a name="tools-and-repos"></a>

### Official Polymarket
- **py-clob-client**: Python client for Polymarket CLOB API
  - https://github.com/Polymarket/py-clob-client
- **rs-clob-client**: Rust CLOB client (for low-latency applications)
  - https://github.com/Polymarket/rs-clob-client
- **Polymarket Agents**: AI agent framework for autonomous trading
  - https://github.com/Polymarket/agents

### Market Making Bots
- **poly-maker**: Automated market making bot with Google Sheets config
  - https://github.com/warproxxx/poly-maker
- **polymarket-market-maker-bot**: Production-ready MM bot with inventory management
  - https://github.com/lorine93s/polymarket-market-maker-bot

### Arbitrage Bots
- **polymarket-arbitrage-bot**: Single/multi-market arbitrage scanner
  - https://github.com/0xalberto/polymarket-arbitrage-bot
- **polymarket-kalshi-btc-arbitrage-bot**: Cross-platform BTC market arb
  - https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot

### Multi-Strategy Bots
- **polybot**: Reverse-engineer strategies + trade execution (paper + live)
  - https://github.com/ent0n29/polybot
- **polymarket-trading-bot**: Beginner-friendly Python bot with flash crash strategy
  - https://github.com/discountry/polymarket-trading-bot
- **Polymarket-betting-bot**: Copy trading + odds-based strategy bots
  - https://github.com/echandsome/Polymarket-betting-bot

### Weather Trading
- **simmer-weather**: Automated weather market trading using NOAA forecasts
  - Available via Simmer SDK / OpenClaw framework
- **NOAA API**: Free weather forecast data (api.weather.gov)

### Analytics
- **Polymarket Analytics**: Trader leaderboard and market data
  - https://polymarketanalytics.com
- **Polywhaler**: Whale/insider tracker
  - https://www.polywhaler.com
- **Dune Analytics**: On-chain Polymarket dashboards
  - https://dune.com/filarm/polymarket-activity

---

## Sources <a name="sources"></a>

### Academic Papers
- [Application of the Kelly Criterion to Prediction Markets](https://arxiv.org/html/2412.14144v1) - arXiv 2024
- [The Anatomy of Polymarket: Evidence from the 2024 Presidential Election](https://arxiv.org/html/2603.03136) - arXiv 2026
- [How Manipulable Are Prediction Markets?](https://arxiv.org/html/2503.03312v1) - arXiv 2025
- [Price Formation in Field Prediction Markets](https://www.sciencedirect.com/science/article/pii/S1386418123000794) - ScienceDirect
- [Making Profit in a Prediction Market](https://link.springer.com/chapter/10.1007/978-3-642-32241-9_47) - Springer
- [On Optimal Betting Strategies with Multiple Mutually Exclusive Outcomes](https://onlinelibrary.wiley.com/doi/full/10.1111/boer.12474) - Wiley 2025
- [Predictive Market Making via Machine Learning](https://link.springer.com/article/10.1007/s43069-022-00124-0) - Springer
- [Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets](https://arxiv.org/abs/2508.03474) - arXiv
- [Explaining the Favorite-Longshot Bias](https://www.nber.org/system/files/working_papers/w15923/w15923.pdf) - NBER Working Paper
- [Risk Aversion and Favourite-Longshot Bias](https://onlinelibrary.wiley.com/doi/10.1111/ecca.12500) - Economica

### Industry Reports and Analysis
- [Polymarket 2025 Six Major Profit Models](https://www.chaincatcher.com/en/article/2233047) - ChainCatcher
- [Systematic Edges in Prediction Markets](https://quantpedia.com/systematic-edges-in-prediction-markets/) - QuantPedia
- [Polymarket Strategies: 2026 Guide](https://cryptonews.com/cryptocurrency/polymarket-strategies/) - CryptoNews
- [Polymarket Strategy 2026: Why Arbitrage Is Dead](https://www.tradetheoutcome.com/polymarket-strategy-2026/) - TradeTheOutcome
- [6 Ways to Make Money in Prediction Markets](https://unchainedcrypto.com/6-easy-ways-to-make-money-in-prediction-markets-in-2026/) - Unchained
- [Market Making on Prediction Markets Guide](https://newyorkcityservers.com/blog/prediction-market-making-guide) - NYC Servers
- [Advanced Prediction Market Trading Strategies](https://metamask.io/news/advanced-prediction-market-trading-strategies) - MetaMask
- [Prediction Market Arbitrage Guide](https://newyorkcityservers.com/blog/prediction-market-arbitrage-guide) - NYC Servers
- [Understanding the Polymarket Fee Curve](https://quantjourney.substack.com/p/understanding-the-polymarket-fee) - QuantJourney

### Weather Market Strategies
- [Weather Trading Bots Making $24,000 on Polymarket](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09) - Dev Genius
- [Making Millions on Polymarket Betting on Weather](https://ezzekielnjuguna.medium.com/people-are-making-millions-on-polymarket-betting-on-the-weather-and-i-will-teach-you-how-24c9977b277c) - Medium
- [Polymarket Traders Profit from Weather Predictions](https://phemex.com/news/article/polymarket-traders-profit-from-weather-predictions-58213) - Phemex

### Whale Tracking and Copy Trading
- [How To Find The BEST Polymarket Wallets To Copy Trade](https://medium.com/@0xmega/how-to-find-the-best-polymarket-wallets-to-copy-trade-without-getting-rekt-26dd65123324) - Medium
- [Tracking High-Probability Wallets on Polymarket](https://phemex.com/news/article/tracking-highprobability-wallets-on-polymarket-for-strategic-insights-63605) - Phemex
- [How to Track Polymarket Wallets](https://laikalabs.ai/prediction-markets/how-to-track-polymarket-wallets) - Laika Labs

### Kelly Criterion
- [Simple Kelly Betting in Prediction Markets](https://www.lesswrong.com/posts/eBGAsxWGKzHsTNRxQ/simple-kelly-betting-in-prediction-markets) - LessWrong
- [Kelly Criterion - Stanford](https://crypto.stanford.edu/~blynn/pr/kelly.html)
- [Learning Performance of Prediction Markets with Kelly Bettors](https://people.cs.umass.edu/~wallach/workshops/nips2011css/papers/Beygelzimer.pdf) - UMass

### Market Microstructure
- [Polymarket CLOB Documentation](https://docs.polymarket.com/developers/CLOB/introduction)
- [Avellaneda & Stoikov Market-Making Strategy Guide](https://medium.com/hummingbot/a-comprehensive-guide-to-avellaneda-stoikovs-market-making-strategy-102d64bf5df6) - Hummingbot
- [Market Making in Prediction Markets](https://www.quantvps.com/blog/market-making-in-prediction-markets) - QuantVPS
