"""
Favorite-Longshot Bias Exploitation v2.0 (Quant Rewrite)

Academic foundation:
  Snowberg & Wolfers (2010) — power function model for probability distortion:
    p_true = p^alpha / (p^alpha + (1-p)^alpha)
  alpha > 1 implies the favorite-longshot bias:
    - Favorites (p > 0.5) are underpriced -> p_true > p_market
    - Longshots (p < 0.5) are overpriced -> p_true < p_market

  With alpha = 1.12 (conservative for prediction markets):
    p=0.70 -> p_true=0.712 -> edge=1.2%
    p=0.80 -> p_true=0.818 -> edge=1.8%
    p=0.85 -> p_true=0.870 -> edge=2.0%
    p=0.90 -> p_true=0.921 -> edge=2.1%

Market Efficiency Score: markets with higher liquidity/volume/tighter spread
are more efficient and require larger edge to trade.

Sizing: Half-Kelly f* = 0.5 * (p_true - price) / (1 - price).

References:
  Snowberg, Wolfers (2010) — Explaining the Favorite-Longshot Bias
  Rothschild (2009) — Forecasting Elections
  Thaler, Ziemba (1988) — Parimutuel Betting Markets
"""

import logging
import math
import time
from dataclasses import dataclass
from utils.risk_manager import Trade

logger = logging.getLogger(__name__)

BIAS_CATEGORIES = {"politics", "pop-culture", "entertainment", "science",
                   "world", "geopolitics", "elections", ""}

EXCLUDE_KEYWORDS = [
    "temperature", "weather", "highest temp", "lowest temp", "precipitation",
    "bitcoin price", "btc price", "eth price", "crypto price",
    "bitcoin", "ethereum", "solana", "xrp",
    "nba", "nfl", "mlb", "nhl", "premier league", "champions league",
    "serie a", "la liga", "bundesliga", "ligue 1", "copa",
    "world cup", "olympic", "grand slam", "grand prix", "formula 1",
    "f1", "ufc", "boxing", "tennis", "cricket", "rugby",
    " win on ", " win the ", " beat ", " defeat ",
    "finals", "playoff", "super bowl", "march madness", "ncaa",
    "gold (gc)", "silver", "crude oil", "wti", "brent",
    "settle at", "futures",
    # Sports patterns (team vs team, over/under, spread, moneyline)
    " vs. ", " vs ", "o/u ", "over/under", "spread", "moneyline",
    "predators", "sabres", "lakers", "celtics", "warriors", "yankees",
]


def _implied_true_prob(p_market: float, alpha: float = 1.12) -> float:
    """
    Snowberg-Wolfers (2010) power function model.

    p_true = p^alpha / (p^alpha + (1-p)^alpha)

    alpha > 1 -> favorite-longshot bias:
      favorites are underpriced (p_true > p_market)
      longshots are overpriced (p_true < p_market)

    alpha = 1.12 is conservative for prediction markets
    (horse racing uses 1.3-1.5, prediction markets are more efficient).
    """
    if p_market <= 0.01 or p_market >= 0.99:
        return p_market
    pa = p_market ** alpha
    qa = (1.0 - p_market) ** alpha
    return pa / (pa + qa)


def _market_efficiency(market) -> float:
    """
    Market efficiency score [0, 1].
    Higher = more efficient = less exploitable edge.

    Based on:
    - Spread tightness (closer to sum=1.0 = more efficient)
    - Liquidity depth (more capital = more efficient)
    - Volume (more traded = more price discovery)
    """
    price_sum = market.prices.get("yes", 0.5) + market.prices.get("no", 0.5)
    spread = abs(1.0 - price_sum)
    spread_score = max(0.0, 1.0 - spread / 0.04)

    liq = getattr(market, "liquidity", 0) or 0
    liq_score = min(liq / 100_000, 1.0)

    vol = getattr(market, "volume", 0) or 0
    vol_score = min(vol / 500_000, 1.0)

    return spread_score * 0.4 + liq_score * 0.3 + vol_score * 0.3


@dataclass
class FavoriteLongshotOpportunity:
    market_id: str
    question: str
    side: str
    price: float
    p_true: float       # Snowberg-Wolfers true probability
    edge: float          # p_true - price (net of spread)
    kelly_fraction: float
    target_size: float
    token_id: str
    volume: float
    category: str
    efficiency: float    # market efficiency score
    alpha: float         # bias parameter used


class FavoriteLongshotStrategy:
    """Exploits the favorite-longshot bias with Snowberg-Wolfers model."""

    MIN_PRICE = 0.70
    MAX_PRICE = 0.90
    BASE_ALPHA = 1.12      # baseline bias parameter
    MIN_VOLUME = 50_000
    MIN_LIQUIDITY = 1_000
    MIN_EDGE = 0.01        # 1% minimum edge after spread + efficiency
    BANKROLL = 1000.0      # dedicated bankroll for this strategy
    MAX_BET = 40.0
    MIN_BET = 10.0
    MAX_POSITIONS = 10
    SCAN_INTERVAL = 1800
    COOLDOWN_PER_MARKET = 86400

    def __init__(self):
        self._last_scan = 0.0
        self._positions: dict[str, float] = {}
        self._total_profit = 0.0
        self._total_trades = 0

    def _is_excluded(self, market) -> bool:
        q = market.question.lower()
        return any(kw in q for kw in EXCLUDE_KEYWORDS)

    def _estimate_alpha(self, market) -> float:
        """
        Estimate bias strength (alpha) based on market characteristics.

        More retail participation = stronger bias = higher alpha.
        More efficient market = weaker bias = lower alpha.

        v11.0: Time-decaying alpha — retail bias weakens as market
        approaches resolution (price discovery converges). Inspired by
        Qlib's alpha decay tracking and Snowberg-Wolfers empirical finding
        that bias is strongest in early trading.

        α_t = α_base * (1 - (t/T)^2) where t = time elapsed, T = total duration.
        Quadratic decay: slow initially, accelerates near resolution.
        """
        alpha = self.BASE_ALPHA

        vol = getattr(market, "volume", 0) or 0
        if vol > 500_000:
            alpha += 0.04    # high retail volume = stronger bias
        elif vol < 100_000:
            alpha -= 0.03    # low activity = less retail bias

        # Efficiency adjustment: more efficient markets have less bias
        eff = _market_efficiency(market)
        alpha -= eff * 0.05  # max -0.05 for very efficient markets

        # v11.0: Time decay — bias fades as market nears resolution
        end_date = getattr(market, "end_date", None)
        if end_date:
            try:
                now = time.time()
                # end_date could be ISO string or timestamp
                if isinstance(end_date, str):
                    from datetime import datetime, timezone
                    end_ts = datetime.fromisoformat(
                        end_date.replace("Z", "+00:00")
                    ).timestamp()
                else:
                    end_ts = float(end_date)

                remaining = max(0, end_ts - now)
                total_duration = 30 * 86400  # assume 30-day market as reference
                # Fraction of life remaining: 1.0 = just opened, 0.0 = about to resolve
                life_frac = min(1.0, remaining / total_duration)
                # Quadratic decay: alpha_excess decays as (life_frac)^2
                alpha_excess = alpha - 1.0
                alpha = 1.0 + alpha_excess * (0.3 + 0.7 * life_frac ** 2)
                # At 100% life remaining: alpha = base (full bias)
                # At 50% remaining: alpha = 1 + excess * 0.475 (~56% bias)
                # At 0% remaining: alpha = 1 + excess * 0.3 (30% residual bias)
            except (ValueError, TypeError, OverflowError):
                pass  # can't parse end_date, use unadjusted alpha

        return max(alpha, 1.01)  # alpha must be > 1 for bias to exist

    def scan(self, markets: list, risk=None) -> list[FavoriteLongshotOpportunity]:
        now = time.time()
        if now - self._last_scan < self.SCAN_INTERVAL:
            return []
        self._last_scan = now

        active = sum(1 for ts in self._positions.values()
                     if now - ts < self.COOLDOWN_PER_MARKET)
        if active >= self.MAX_POSITIONS:
            logger.debug(f"[FAV-LONG] Max positions ({self.MAX_POSITIONS}) reached")
            return []

        opportunities = []
        scanned = 0

        for m in markets:
            if not m.active:
                continue
            if self._is_excluded(m):
                continue
            if m.volume < self.MIN_VOLUME:
                continue
            if m.liquidity < self.MIN_LIQUIDITY:
                continue
            if m.condition_id in self._positions:
                if now - self._positions[m.condition_id] < self.COOLDOWN_PER_MARKET:
                    continue

            # v12.0.1: check risk manager for existing positions (survives restart)
            if risk:
                existing = [t for t in risk.open_trades
                            if t.market_id == m.condition_id or
                            t.token_id in (m.tokens.get("yes", ""), m.tokens.get("no", ""))]
                if existing:
                    continue

            scanned += 1

            # Find the favorite side
            p_yes = m.prices.get("yes", 0.5)
            p_no = m.prices.get("no", 0.5)

            if p_yes >= p_no:
                fav_side, fav_price = "YES", p_yes
                token_id = m.tokens.get("yes", "")
            else:
                fav_side, fav_price = "NO", p_no
                token_id = m.tokens.get("no", "")

            if fav_price < self.MIN_PRICE or fav_price > self.MAX_PRICE:
                continue
            if not token_id:
                continue

            # ── Snowberg-Wolfers: compute true probability ──
            alpha = self._estimate_alpha(m)
            p_true = _implied_true_prob(fav_price, alpha)
            raw_edge = p_true - fav_price

            # Spread cost
            spread_cost = m.spread / 2 if hasattr(m, "spread") else 0.01

            # Efficiency-adjusted minimum edge: efficient markets need more
            eff = _market_efficiency(m)
            adj_min_edge = self.MIN_EDGE * (1.0 + eff * 0.5)

            net_edge = raw_edge - spread_cost
            if net_edge < adj_min_edge:
                continue

            # ── Half-Kelly sizing ──
            # f* = (p_true - price) / (1 - price)
            kelly_full = max(0.0, (p_true - fav_price) / (1.0 - fav_price))
            kelly_half = kelly_full * 0.5

            size = self.BANKROLL * kelly_half
            size = max(self.MIN_BET, min(size, self.MAX_BET))

            opp = FavoriteLongshotOpportunity(
                market_id=m.condition_id,
                question=m.question[:80],
                side=fav_side,
                price=fav_price,
                p_true=p_true,
                edge=net_edge,
                kelly_fraction=kelly_half,
                target_size=size,
                token_id=token_id,
                volume=m.volume,
                category=m.category,
                efficiency=eff,
                alpha=alpha,
            )
            opportunities.append(opp)

        # Sort by edge (highest first)
        opportunities.sort(key=lambda o: o.edge, reverse=True)

        remaining_slots = self.MAX_POSITIONS - active
        opportunities = opportunities[:remaining_slots]

        if scanned > 0:
            logger.info(
                f"[FAV-LONG] Scanned {scanned} eligible markets -> "
                f"{len(opportunities)} opportunities "
                f"(active={active}/{self.MAX_POSITIONS})"
            )

        return opportunities

    def execute(self, opp: FavoriteLongshotOpportunity, api, risk=None,
                live: bool = False) -> bool:
        size = opp.target_size

        if not live:
            self._positions[opp.market_id] = time.time()
            self._total_trades += 1
            logger.info(
                f"[FAV-LONG] PAPER BUY {opp.side} ${size:.0f} @{opp.price:.2f} "
                f"p_true={opp.p_true:.3f} edge={opp.edge:.3f} "
                f"alpha={opp.alpha:.2f} eff={opp.efficiency:.2f} "
                f"kelly={opp.kelly_fraction:.4f} '{opp.question[:50]}'"
            )
            return True

        try:
            result = api.smart_buy(
                token_id=opp.token_id,
                amount=size,
                target_price=opp.price,
            )
            if result:
                self._positions[opp.market_id] = time.time()
                self._total_trades += 1
                logger.info(
                    f"[FAV-LONG] BUY {opp.side} ${size:.0f} @{opp.price:.2f} "
                    f"p_true={opp.p_true:.3f} edge={opp.edge:.3f} "
                    f"alpha={opp.alpha:.2f} '{opp.question[:50]}'"
                )
                # v12.0.1: register trade in risk manager
                if risk:
                    trade = Trade(
                        timestamp=time.time(),
                        strategy="favorite_longshot",
                        market_id=opp.market_id,
                        token_id=opp.token_id,
                        side=f"BUY_{opp.side}",
                        size=size,
                        price=opp.price,
                        edge=opp.edge,
                        reason=f"fav-long alpha={opp.alpha:.2f}",
                    )
                    risk.open_trade(trade)
                return True
            else:
                logger.warning(f"[FAV-LONG] Order failed: {opp.question[:50]}")
                return False
        except Exception as e:
            logger.warning(f"[FAV-LONG] Error: {e}")
            return False

    @property
    def stats(self) -> dict:
        return {
            "total_trades": self._total_trades,
            "active_positions": len(self._positions),
        }
