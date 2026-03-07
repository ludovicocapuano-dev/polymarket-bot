# Polymarket Platform Mechanics, Fee Structure, and Exploitable Edges
## Research compiled 2026-03-06

---

## 1. FEE STRUCTURE

### Fee Formula
```
fee = C * p * feeRate * (p * (1 - p))^exponent
```
Where: C = shares traded, p = share price (0 to 1)

### Fee Parameters by Market Type

| Category | Fee Rate | Exponent | Max Effective Fee (at p=0.50) | Maker Rebate % |
|----------|----------|----------|-------------------------------|----------------|
| Crypto (all durations) | 0.25 | 2 | 1.56% | 20% |
| Sports (NCAAB, Serie A) | 0.0175 | 1 | 0.44% | 25% |
| Everything else | 0 | N/A | 0% | N/A |

### Key Fee Properties
- **Quadratic decay (crypto)**: Fee ~ p^2 * (1-p)^2. Falls off VERY fast away from 0.50. At p=0.90, effective fee is ~0.02% (crypto) vs 1.56% at p=0.50
- **Linear decay (sports)**: Fee ~ p * (1-p). At p=0.90, effective fee is ~0.16% (sports)
- **Maker orders are ALWAYS free** — only takers pay
- **Minimum fee**: 0.0001 USDC (below this rounds to zero)
- **Only applies to markets deployed AFTER activation date** (Mar 6, 2026 for crypto; Feb 18, 2026 for NCAAB/Serie A)
- No deposit, withdrawal, or account fees
- Gas fees on Polygon are separate (~$0.01-0.05)

### Mathematical Implication for Strategy
- **Fee-free categories are pure alpha**: Weather, politics, geopolitics, entertainment, science, tech — ZERO trading friction
- **High-probability trades in fee markets are nearly free**: At p=0.95, crypto fee is ~0.006%. Resolution sniping at high-prob remains viable even in fee markets
- **Mid-price trades in crypto markets are expensive**: 1.56% at p=0.50 kills most market-making and event-driven edges
- **Sports fees are manageable**: 0.44% max is workable for edges > 2%

---

## 2. FEE-FREE MARKETS AND WHY

### Fee-Free Categories (as of March 2026)
- **Politics** (elections, legislation, appointments)
- **Weather** (temperature, precipitation, natural disasters)
- **Geopolitics** (wars, treaties, sanctions)
- **Entertainment** (awards, ratings, releases)
- **Science/Tech** (launches, discoveries, AI)
- **Economics/Macro** (GDP, inflation, employment — non-crypto)
- **Crypto long-term** markets deployed BEFORE Mar 6, 2026

### Why Fee-Free
Polymarket's stated rationale: fees were introduced specifically to combat **latency arbitrage** in short-duration crypto markets, where HFT bots exploited the delay between Binance/Coinbase price updates and Polymarket oracle updates. Sports fees combat broadcast delay exploitation. Fee-free markets don't have these oracle-latency attack vectors.

### Mathematical Implication
- **Concentrate capital in fee-free categories**: Weather strategy's 90% allocation is correct — zero friction maximizes Kelly growth rate
- **Fee-free markets have the same payout structure** ($1 per winning share) but zero cost to enter/exit
- **Edge threshold is lower**: In fee-free markets, any edge > spread cost is profitable. In crypto markets at p=0.50, need edge > 1.56% + spread

---

## 3. ORDER BOOK DYNAMICS AND LIQUIDITY PATTERNS

### CLOB Architecture
- **Hybrid-decentralized**: Off-chain order matching, on-chain settlement on Polygon
- **Non-custodial**: Users sign EIP-712 orders; operator matches but cannot execute unauthorized trades
- **Dual-sided book**: Every BUY YES at price X is simultaneously a SELL NO at price (1-X). This creates **synthetic liquidity** — the book is effectively 2x deeper than it appears
- **Order types**: Limit orders only (GTC, GTD). No market orders — "market" buys are limit orders at best ask

### Liquidity Patterns
- **High liquidity**: Politics (especially US elections), top crypto markets (BTC/ETH price), major sports
- **Low liquidity**: Weather, science, niche geopolitics, long-dated markets
- **Spread patterns**: Popular markets 1-3%, niche markets 5-15%, dead markets 20%+
- **Volume concentration**: Top 10% of markets account for ~80% of volume
- **Time-of-day effects**: US market hours (14:00-22:00 UTC) have highest liquidity

### API Structure
- **Gamma API**: Market metadata and discovery (conditions, outcomes, categories)
- **CLOB API**: Trading operations (place/cancel orders, get orderbook)
- **Data API**: User-specific data (positions, trade history, P&L)
- **Authentication**: L1 (EIP-712 wallet signature) → L2 (HMAC-SHA256 with derived API key/secret/passphrase)

### Mathematical Implication
- **Dual-book creates hidden opportunities**: A mispriced YES is simultaneously a mispriced NO. Monitor both sides
- **Low-liquidity markets have wider edges but execution risk**: Kelly sizing must account for partial fills
- **Spread cost is the true fee in fee-free markets**: Calibrate spread_cost dynamically per market (already done in v10.1)

---

## 4. RESOLUTION MECHANISM

### UMA Optimistic Oracle Process
1. **Proposal**: Anyone posts $750 USDC.e bond + proposed outcome
2. **Challenge Period**: 2 hours — anyone can dispute
3. **If unchallenged**: Market resolves to proposed outcome. Proposer gets bond back + $2 reward
4. **If challenged**: Escalated to UMA DVM (Data Verification Mechanism)
5. **DVM Vote**: UMA tokenholders vote over 48 hours with hidden ballots
6. **Final Resolution**: Winning shares pay $1, losing shares pay $0

### Resolution Timing
- **Proposal → Resolution (unchallenged)**: ~2 hours
- **Proposal → Resolution (challenged)**: ~50 hours (2h challenge + 48h DVM)
- **Event → Proposal**: Variable. Can be seconds (automated) or hours/days (manual)

### Edge Cases
- **Early resolution proposals lose bond**: If outcome isn't yet deterministic, proposer loses $750
- **Ambiguous resolution rules**: Some markets have vague criteria — creates dispute risk
- **Multiple valid interpretations**: DVM vote is subjective, creating outcome uncertainty

### Mathematical Implication
- **2-hour challenge window = trading window**: After proposal, price should converge to $1.00/$0.00 but often doesn't immediately. Resolution sniping exploits this gap
- **$750 bond = barrier to manipulation**: Too expensive to spam false proposals, but profitable for correct early proposals ($2 reward per correct proposal)
- **Challenge risk is ~5%**: Based on historical data, ~5% of proposals are challenged. This means resolution sniping has ~95% win rate (matches existing strategy's parameters)

---

## 5. RESOLUTION TIMING EXPLOITS

### Known Exploits (Historical)

#### Temporal/Latency Arbitrage (Crypto Markets)
- Trader "0x8dxd" earned $515K/month exploiting delay between Binance/Coinbase prices and Polymarket oracle updates
- 7,300+ trades, 99% win rate
- **Now countered**: Dynamic taker fees up to 3.15% at p=0.50 in crypto markets

#### Broadcast Delay Exploitation (Sports)
- Algorithm turned $5 into $3.7M exploiting 15-40 second lag between live stadium data and TV broadcast
- Used real-time stadium APIs to trade before market reacted
- **Now countered**: 3-second delay on marketable orders in sports markets + fees

#### Oracle Latency (Hourly Crypto)
- $50K/week exploiting lag in hourly market resolutions
- **Now countered**: Fees on all crypto market durations as of Mar 6, 2026

#### Resolution Sniping (Still Viable)
- Buy when UMA proposal is submitted but market price hasn't fully adjusted
- Edge: 5-15%, Win rate: ~95%
- **NOT countered by fees**: Works in fee-free markets (politics, weather) AND in fee markets where the edge exceeds the fee

### Mathematical Implication
- **Resolution sniping is the last reliable timing exploit**: All latency arbitrage has been patched with fees/delays
- **Expected value of resolution snipe**: EV = 0.95 * edge - 0.05 * loss_if_disputed. For 10% edge with $100 position: EV = 0.95 * $10 - 0.05 * $100 = $4.50
- **Optimal capital**: Deploy maximum Kelly-sized position immediately when proposal is detected and market price confirms edge > threshold

---

## 6. CLOB MECHANICS (TECHNICAL)

### Order Lifecycle
1. User signs EIP-712 order (price, size, side, tokenID, expiration)
2. Order submitted to off-chain CLOB operator
3. Operator matches orders by price-time priority
4. Matched orders submitted on-chain to Exchange contract
5. Atomic swap: collateral (USDC) ↔ outcome tokens

### Key Technical Details
- **Tick size**: $0.01 (1 cent)
- **Minimum order**: $1.00
- **Wallet types**: EOA (type 0), POLY_PROXY (type 1, Magic Link), GNOSIS_SAFE (type 2, most common)
- **Order cancellation**: Can be done on-chain (gasless via operator) or directly on-chain (costs gas but guaranteed)
- **Nonce management**: Each order has a nonce; incrementing wallet nonce invalidates all pending orders (this is the incrementNonce() exploit that killed arb strategies)

### SDKs
- Python: `py-clob-client` (pip install py-clob-client)
- TypeScript: `@polymarket/clob-client`
- Rust client also available

### Mathematical Implication
- **Price-time priority means speed matters for popular markets**: For competitive markets, latency < 100ms is needed
- **$0.01 tick creates minimum 1% spread** in binary markets: Cannot have spread < $0.01
- **incrementNonce() risk**: Any multi-leg strategy where legs are submitted sequentially (not atomically) is vulnerable to the nonce being incremented between legs, leaving you with a naked position

---

## 7. CROSS-MARKET CORRELATION OPPORTUNITIES

### Types of Arbitrage Identified

#### Within-Market Rebalancing
- In negRisk markets, sum of YES prices should = ~$1.00
- When sum < $1.00: buy all YES tokens → guaranteed profit
- When sum > $1.00: sell all YES tokens → guaranteed profit
- **Scale**: $10.6M + $4.7M = $15.3M extracted (Apr 2024 - Apr 2025)

#### Combinatorial/Cross-Market Arbitrage
- Logically dependent markets: "Will X happen?" + "If X happens, will Y?"
- If conditional probabilities are inconsistent, arbitrage exists
- **Scale**: $95K identified across dependent pairs (smaller but lower competition)

#### Cross-Platform Arbitrage
- Polymarket vs Kalshi vs Opinion vs PredictIt
- Same event, different prices → buy cheap side on one platform, sell expensive on another
- **Challenge**: Combined fees (5%+) often eat the edge. Need > 5% spread to profit
- **Scale**: ~$40M total arbitrage profit estimated across all types (Apr 2024 - Apr 2025)

### Current State
- Simple YES+NO < $1.00 gaps closed by bots in milliseconds
- Cross-market semantic arbitrage (logically related markets) still exists but requires NLP detection
- Cross-platform remains viable for large dislocations (>5% spread)

### Mathematical Implication
- **NegRisk sum monitoring is free alpha**: If SUM(YES prices) < 0.98 in a negRisk market, buy all. Guaranteed 2%+ return
- **Conditional probability mismatches**: P(A and B) should equal P(A) * P(B|A). When Polymarket markets imply different values, arbitrage exists
- **Execution risk dominates**: Multi-leg arb with sequential execution has gap risk. Need atomic execution or accept 3-5s exposure

---

## 8. NEGATIVE RISK MARKETS

### How They Work
- **Multi-outcome markets** where exactly ONE outcome resolves YES
- Examples: "Who wins the 2028 election?" (Trump, Harris, DeSantis, etc.)
- Each outcome is a separate binary market (YES/NO)
- **Key conversion**: 1 NO share of any outcome can be converted to 1 YES share of every OTHER outcome

### Capital Efficiency
- **Standard markets**: Buying all N outcomes costs SUM(prices). E.g., 5 outcomes at $0.30 each = $1.50 collateral
- **NegRisk markets**: Buying all N outcomes costs MAX(1.0) collateral regardless of individual prices
- **Savings**: (SUM - 1.0) / SUM = collateral reduction percentage

### Smart Contract Architecture
- **Neg Risk Adapter**: Handles conversion between NO tokens and YES tokens across outcomes
- **Neg Risk CTF Exchange**: Dedicated exchange for negRisk markets
- API flag: `negRisk: true` in order options

### Rebalancing Extraction
- Study found $29M extracted through negRisk market rebalancing
- Mechanism: When outcome prices drift (sum != $1.00), arbitrageurs buy underpriced set and convert

### Mathematical Implication
- **Capital efficiency multiplier**: In a 10-outcome market, standard collateral = SUM(prices) ≈ $1.00 + noise. NegRisk collateral = $1.00 always. This means you can deploy more capital per dollar of edge
- **Conversion formula**: 1 NO_i → {YES_1, YES_2, ..., YES_N} \ {YES_i}. Cost = price(NO_i). Revenue = SUM(price(YES_j)) for j != i
- **Arbitrage condition**: If price(NO_i) < SUM(price(YES_j)) for j != i, conversion is profitable
- **Equivalent condition**: If SUM(all YES prices) > $1.00, sell the overpriced set. If < $1.00, buy the underpriced set

---

## 9. REWARD PROGRAMS

### A. Holding Rewards (4% Annualized)
- **Rate**: 4.00% APY (variable, can change)
- **Eligible markets**: 13 specific long-term markets (2028 US presidential, 2026 midterms, Russia/China/Turkey/Israel/Ukraine leadership)
- **Calculation**: Position value sampled hourly (random time), averaged daily
- **Position value**: shares * mid_price at sample time
- **Payout**: Daily in USDC from Polymarket Treasury
- **No minimum**: Any position size qualifies

#### Mathematical Implication
- **Free carry on positions you'd hold anyway**: If holding a 2028 election position, the 4% APY is pure bonus
- **Optimal strategy**: Hold large positions in eligible markets where you have directional conviction. The 4% APY acts as a "cost of carry" subsidy
- **Annualized**: $1000 position earns ~$0.11/day = ~$40/year. Not huge but risk-free addition to any directional position

### B. Liquidity Rewards Program
- **Eligibility**: Any maker with resting limit orders that get filled
- **Distribution**: Daily at midnight UTC
- **Scoring formula**: S(v,s) = ((v-s)/v)^2 * b
  - v = maximum allowable spread
  - s = actual spread from adjusted midpoint
  - b = order size
- **Quadratic penalty**: Being 2x tighter than competitors = ~4x the rewards (not 2x)
- **Two-sided bonus**: Having orders on BOTH sides of the book earns more than one side
- **Single-sided penalty**: When midpoint is 0.10-0.90, single-sided liquidity scored at 1/3 rate (divided by c=3.0)
- **Extreme prices**: When midpoint > 0.90 or < 0.10, ONLY two-sided liquidity qualifies
- **Minimum payout**: $1.00 (below this = nothing)
- **Sampling**: Minute-level snapshots across 7-day epochs (10,080 samples)

#### Mathematical Implication
- **Quadratic rewards favor TIGHT spreads**: A market maker quoting 1% spread earns ~16x more than one quoting 4% spread (per the quadratic function)
- **Two-sided quoting is mandatory for extreme prices**: At p > 0.90 or p < 0.10, single-sided orders earn $0
- **Optimal strategy**: Quote tight two-sided markets in high-reward pools. The rewards can exceed the spread P&L, making it profitable to quote tighter than otherwise rational
- **Competition matters**: Your rewards = your_score / total_all_makers_score. If others quote tighter, your share drops

### C. Maker Rebates Program
- **Funded by**: Taker fees collected in fee-enabled markets
- **Eligible markets**: Crypto (20% of fees), NCAAB/Serie A (25% of fees)
- **Formula**: rebate = (your_fee_equivalent / total_fee_equivalent) * rebate_pool
- **Distribution**: Daily in USDC

#### Mathematical Implication
- **Maker rebates partially offset adverse selection**: If you're a maker getting filled by informed takers, the 20-25% rebate helps absorb some of the loss
- **Net fee for makers in crypto**: Maker pays 0% but EARNS rebate when their orders are filled. Being a maker in fee markets is subsidized
- **Combined with liquidity rewards**: A maker in crypto markets earns BOTH liquidity rewards AND maker rebates = double income stream

### D. Permissionless Market Rewards (New Feb 2026)
- Any user can **sponsor** rewards on any market to attract liquidity
- Enables getting the orderbook depth you need for large trades
- Coming soon: permissionless market deployment + creator fees

---

## SUMMARY: EXPLOITABLE EDGES BY PRIORITY

| Edge | Category | Fee Impact | Est. Edge | Risk | Status |
|------|----------|-----------|-----------|------|--------|
| Weather markets | Fee-free | 0% | 5-15% | Low | ACTIVE (90% allocation) |
| Resolution sniping | Any | 0-1.56% | 5-15% | ~5% dispute | ACTIVE (10% allocation) |
| Holding rewards carry | Fee-free eligible | 0% | 4% APY | Near-zero | SHOULD EXPLOIT |
| Liquidity rewards (fee-free mkts) | Fee-free | 0% | Variable | Adverse selection | VIABLE if budget > $2K |
| NegRisk sum arbitrage | Any | Varies | 1-5% | Execution | VIABLE with atomic execution |
| Cross-platform arb | N/A | 5%+ combined | 5-10% | Execution + fees | MARGINAL |
| Latency arbitrage | Crypto/Sports | 1.56-3.15% | DEAD | N/A | PATCHED |
| Cross-market semantic arb | Any | 0-1.56% | 1-3% | Detection + execution | VIABLE with NLP |
| Maker rebates farming | Fee markets | Net positive | Variable | Adverse selection | VIABLE if making markets |

### Top Recommendations for This Bot
1. **Continue weather + resolution_sniper** — fee-free, proven edges
2. **Add holding rewards tracking** — free 4% APY on eligible long-term positions
3. **Monitor negRisk sum deviations** — automated alert when SUM(YES) deviates from $1.00
4. **Consider fee-aware Kelly sizing** — net-of-fees edge calculation for any expansion into fee markets
5. **Avoid crypto short-duration markets** — fees (up to 3.15%) exceed most edges at mid-prices
