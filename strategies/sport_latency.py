"""
Strategia: Sport Latency Arbitrage — v1.0
==========================================
Sfrutta il lag di 10-15s tra eventi sportivi reali e aggiornamento
prezzi su Polymarket. Betfair Exchange reagisce in 1-3s (sharp bettors
in stadio), Polymarket in 10-15s (broadcast delay + oracle consensus).

Tre modalita' di trading:
1. GOAL SNIPER: shift >30% implied prob = goal/knockout. High confidence.
2. SET PIECE: shift 15-30% = penalty/red card. Medium confidence.
3. MOMENTUM: shift 8-15% sostenuto 60s = tactical advantage. Lower confidence.

Fair value = Betfair implied probability (overround-adjusted).
Edge = Betfair fair value - Polymarket stale price.
Fee = 0 (sport markets are fee-free on Polymarket).

Riferimenti:
  Article: "How I Front-Ran Polymarket by 12 Seconds"
  Betfair Exchange streaming for indirect event detection
"""

import difflib
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field

from utils.polymarket_api import Market, PolymarketAPI
from utils.betfair_feed import BetfairFeed, EventSignal
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "sport_latency"

# Reuse sport keywords from crowd_sport for Polymarket market identification
SPORT_KEYWORDS = [
    "nba", "nfl", "nhl", "mlb", "ipl", "premier league",
    "champions league", "la liga", "bundesliga", "serie a",
    "ligue 1", "copa", "stanley cup", "super bowl",
    "world series", "ufc", "boxing", "tennis", "wimbledon",
    "grand slam", "grand prix", "formula 1", "f1",
    "cricket", "rugby", "ncaa", "march madness",
    "playoffs", "finals", "world cup", "olympic",
    "win the", "beat", "defeat",
    "lakers", "celtics", "warriors", "knicks", "bulls", "bucks",
    "nuggets", "suns", "76ers", "heat", "nets", "clippers",
    "yankees", "dodgers", "chiefs", "eagles", "49ers",
    "manchester", "liverpool", "barcelona", "real madrid",
    "arsenal", "chelsea", "bayern", "juventus", "inter",
    "psg", "napoli", "ac milan", "tottenham", "spurs",
]

# Team name normalization: strip common suffixes for fuzzy matching
STRIP_SUFFIXES = [
    " fc", " cf", " sc", " united", " city",
    " town", " rovers", " wanderers", " athletic",
]


@dataclass
class SportLatencySignal:
    """Signal from Betfair odds movement vs Polymarket price."""
    market: Market
    event_signal: EventSignal
    mode: str                   # "goal_sniper", "set_piece", "momentum"
    side: str                   # "YES" or "NO"
    fair_prob: float            # from Betfair (overround-adjusted)
    polymarket_price: float     # current Polymarket price (stale)
    edge: float                 # fair_prob - polymarket_price
    confidence: float
    kelly_fraction: float
    target_size: float
    reasoning: str


@dataclass
class SportLatencyStrategy:
    """
    Sport Latency Arbitrage via Betfair Exchange odds.

    Detects in-game events from Betfair odds shifts,
    compares to stale Polymarket prices, trades the gap.
    """
    api: PolymarketAPI
    risk: RiskManager
    betfair: BetfairFeed

    # Parameters
    bankroll: float = 500.0
    max_size: float = 100.0
    min_edge_goal: float = 0.08
    min_edge_set_piece: float = 0.06
    min_edge_momentum: float = 0.05
    daily_loss_limit: float = 200.0
    max_concurrent: int = 5
    cooldown_per_event: float = 60.0

    # State
    _match_cache: dict = field(default_factory=dict)       # betfair_event_id -> (Market | None, expire_ts)
    _recently_traded: dict = field(default_factory=dict)    # event_key -> timestamp
    _momentum_history: dict = field(default_factory=dict)   # betfair_event_id -> deque of EventSignal
    _daily_pnl: float = 0.0
    _daily_pnl_reset_at: float = 0.0
    _trades_executed: int = 0
    _pnl_tracker: dict = field(default_factory=dict)
    _open_positions: int = 0

    # ══════════════════════════════════════════════════════════════
    #  Market Matching: Betfair <-> Polymarket
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_team(name: str) -> str:
        """Normalize team name for fuzzy matching."""
        name = name.lower().strip()
        for suffix in STRIP_SUFFIXES:
            if name.endswith(suffix):
                name = name[:-len(suffix)].strip()
        return name

    @staticmethod
    def _extract_teams_from_betfair(event_name: str) -> tuple[str, str]:
        """Extract team names from Betfair event name ('Team A v Team B')."""
        # Betfair uses " v " separator
        parts = event_name.split(" v ")
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        # Fallback: try " vs "
        parts = event_name.split(" vs ")
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return event_name.strip(), ""

    @staticmethod
    def _is_sport_market(market: Market) -> bool:
        """Check if a Polymarket market is sport-related."""
        q = (market.question or "").lower()
        return any(kw in q for kw in SPORT_KEYWORDS)

    def _match_to_polymarket(
        self,
        event_signal: EventSignal,
        shared_markets: list[Market] | None,
    ) -> Market | None:
        """
        Match a Betfair event to a Polymarket market.

        Uses fuzzy team name matching with caching.
        Returns the matched Market or None.
        """
        event_id = event_signal.betfair_event_id
        now = time.time()

        # Check cache (1 hour TTL)
        if event_id in self._match_cache:
            cached_market, expire_ts = self._match_cache[event_id]
            if now < expire_ts:
                return cached_market

        if not shared_markets:
            self._match_cache[event_id] = (None, now + 3600)
            return None

        team_a, team_b = self._extract_teams_from_betfair(event_signal.event_name)
        if not team_a:
            self._match_cache[event_id] = (None, now + 3600)
            return None

        norm_a = self._normalize_team(team_a)
        norm_b = self._normalize_team(team_b)

        best_match = None
        best_score = 0.0

        for market in shared_markets:
            if not self._is_sport_market(market):
                continue

            q = market.question.lower() if market.question else ""

            # Direct substring match (most reliable)
            a_in_q = norm_a in q
            b_in_q = norm_b in q if norm_b else True

            if a_in_q and b_in_q:
                best_match = market
                best_score = 1.0
                break

            # Fuzzy match with SequenceMatcher
            if norm_a:
                ratio_a = difflib.SequenceMatcher(None, norm_a, q).ratio()
            else:
                ratio_a = 0

            if norm_b:
                ratio_b = difflib.SequenceMatcher(None, norm_b, q).ratio()
            else:
                ratio_b = 0

            combined = (ratio_a + ratio_b) / 2.0
            if combined > best_score and combined > 0.40:
                best_score = combined
                best_match = market

        # Only accept if score is good enough
        if best_score < 0.60:
            best_match = None

        # Cache result
        self._match_cache[event_id] = (best_match, now + 3600)

        if best_match:
            logger.info(
                f"[SPORT-MATCH] {event_signal.event_name} -> "
                f"Polymarket: {best_match.question[:60]} (score={best_score:.2f})"
            )
        else:
            logger.debug(
                f"[SPORT-MATCH] No match for: {event_signal.event_name}"
            )

        return best_match

    # ══════════════════════════════════════════════════════════════
    #  Fair Value & Edge Calculation
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _calc_fair_prob(event_signal: EventSignal) -> float:
        """
        Calculate fair probability from Betfair odds.

        Removes overround by normalizing implied probabilities to sum=1.
        Returns the fair probability of the team whose odds changed.
        """
        all_sels = event_signal.all_selections
        if not all_sels:
            # Fallback: raw implied prob
            return event_signal.odds_after.implied_prob

        # Sum all implied probs (will be >1 due to overround)
        total_implied = 0.0
        for sel in all_sels.values():
            if sel.back_odds > 0:
                total_implied += 1.0 / sel.back_odds

        if total_implied <= 0:
            return event_signal.odds_after.implied_prob

        # Normalize: fair prob = raw implied / total implied
        raw = event_signal.odds_after.implied_prob
        return raw / total_implied

    def _calc_edge(self, fair_prob: float, poly_price: float, side: str) -> float:
        """Calculate edge. Sport markets are fee-free on Polymarket."""
        if side == "YES":
            return fair_prob - poly_price
        else:
            return (1.0 - fair_prob) - (1.0 - poly_price)

    # ══════════════════════════════════════════════════════════════
    #  Signal Generation
    # ══════════════════════════════════════════════════════════════

    def scan(self, shared_markets: list[Market] | None = None) -> list[SportLatencySignal]:
        """
        Scan for sport latency signals.

        Called every cycle (~3s) by the main bot loop.
        Drains EventSignals from BetfairFeed and generates trading signals.
        """
        if self.betfair._disabled:
            return []

        # Daily PnL reset
        now = time.time()
        if now - self._daily_pnl_reset_at > 86400:
            self._daily_pnl = 0.0
            self._daily_pnl_reset_at = now

        # Check daily loss limit
        if self._daily_pnl <= -self.daily_loss_limit:
            return []

        # Check concurrent positions
        if self._open_positions >= self.max_concurrent:
            return []

        # Get event signals from Betfair
        event_signals = self.betfair.pop_signals()
        if not event_signals:
            return []

        signals = []

        for ev in event_signals:
            # Match to Polymarket market
            poly_market = self._match_to_polymarket(ev, shared_markets)
            if not poly_market:
                continue

            # Calculate fair probability from Betfair odds
            fair_prob = self._calc_fair_prob(ev)

            # Get Polymarket current prices
            poly_yes = poly_market.prices.get("yes", 0.5)
            poly_no = poly_market.prices.get("no", 0.5)

            # Determine which side has edge
            # If Betfair says team is MORE likely than Polymarket thinks -> BUY YES
            # If Betfair says team is LESS likely -> BUY NO
            if ev.direction == "shortened":
                # Team became more likely on Betfair -> BUY YES on Polymarket
                side = "YES"
                buy_price = poly_yes
                edge = self._calc_edge(fair_prob, poly_yes, "YES")
            else:
                # Team became less likely -> BUY NO
                side = "NO"
                buy_price = poly_no
                edge = self._calc_edge(fair_prob, poly_yes, "NO")

            # Classify mode and check min edge
            mode, min_edge = self._classify_mode(ev)

            if edge < min_edge:
                logger.debug(
                    f"[SPORT-LATENCY] Low edge: {ev.event_name} "
                    f"edge={edge:.4f} < min={min_edge:.4f} ({mode})"
                )
                continue

            # For momentum: require 2+ concordant signals in 60s
            if mode == "momentum":
                if not self._check_momentum_sustained(ev):
                    continue

            # Anti-stack: no re-entry on same event
            event_key = f"{ev.betfair_event_id}_{ev.event_type}_{ev.team}"
            if now - self._recently_traded.get(event_key, 0) < self.cooldown_per_event:
                continue

            # Kelly sizing
            confidence = ev.confidence
            win_prob = fair_prob if side == "YES" else (1.0 - fair_prob)
            kelly_full = (win_prob - buy_price) / (1.0 - buy_price) if buy_price < 1.0 else 0

            # Mode-dependent Kelly fraction
            if mode == "goal_sniper":
                kelly_frac = kelly_full * 0.50  # half-Kelly
            elif mode == "set_piece":
                kelly_frac = kelly_full * 0.50
            else:
                kelly_frac = kelly_full * 0.25  # quarter-Kelly for momentum

            target_size = max(5.0, min(
                self.bankroll * kelly_frac * confidence,
                self.max_size,
            ))

            # Payoff ratio check
            payoff = (1.0 / buy_price) - 1.0 if buy_price > 0 else 0
            if payoff < 0.10:
                continue  # skip extreme prices (buy_price > 0.91)

            reasoning = (
                f"[{mode.upper()}] {ev.event_name}: {ev.event_type} "
                f"({ev.team} {ev.direction}) | Betfair shift={ev.implied_prob_shift:.3f} "
                f"| fair_P={fair_prob:.3f} vs Poly={buy_price:.3f} "
                f"| edge={edge:.4f} conf={confidence:.2f}"
            )

            signals.append(SportLatencySignal(
                market=poly_market,
                event_signal=ev,
                mode=mode,
                side=side,
                fair_prob=fair_prob,
                polymarket_price=buy_price,
                edge=edge,
                confidence=confidence,
                kelly_fraction=kelly_frac,
                target_size=target_size,
                reasoning=reasoning,
            ))

        # Sort by edge descending
        signals.sort(key=lambda s: s.edge, reverse=True)
        return signals[:2]  # max 2 signals per cycle

    def _classify_mode(self, ev: EventSignal) -> tuple[str, float]:
        """Classify signal into trading mode and return (mode, min_edge)."""
        shift = ev.implied_prob_shift
        if shift >= 0.30:
            return "goal_sniper", self.min_edge_goal
        elif shift >= 0.15:
            return "set_piece", self.min_edge_set_piece
        else:
            return "momentum", self.min_edge_momentum

    def _check_momentum_sustained(self, ev: EventSignal) -> bool:
        """Check if momentum signal is sustained (2+ signals in 60s)."""
        event_id = ev.betfair_event_id
        now = time.time()

        if event_id not in self._momentum_history:
            self._momentum_history[event_id] = deque(maxlen=10)

        history = self._momentum_history[event_id]
        history.append(ev)

        # Count concordant signals in last 60s
        concordant = 0
        for past_ev in history:
            if now - past_ev.timestamp < 60 and past_ev.direction == ev.direction:
                concordant += 1

        return concordant >= 2

    # ══════════════════════════════════════════════════════════════
    #  Execution
    # ══════════════════════════════════════════════════════════════

    async def execute(self, signal: SportLatencySignal, paper: bool = True) -> bool:
        """Execute a sport latency trade."""
        now = time.time()
        event_key = (
            f"{signal.event_signal.betfair_event_id}_"
            f"{signal.event_signal.event_type}_{signal.event_signal.team}"
        )
        if now - self._recently_traded.get(event_key, 0) < self.cooldown_per_event:
            return False

        token_key = signal.side.lower()
        token_id = signal.market.tokens.get(token_key)
        if not token_id:
            logger.warning(f"[SPORT-LATENCY] Token ID non trovato per {token_key}")
            return False

        buy_price = signal.polymarket_price
        size = signal.target_size

        allowed, reason = self.risk.can_trade(
            STRATEGY_NAME, size, price=buy_price,
            side=f"BUY_{signal.side}", market_id=signal.market.id,
        )
        if not allowed:
            logger.info(f"[SPORT-LATENCY] Risk block: {reason}")
            return False

        trade = Trade(
            timestamp=now,
            strategy=STRATEGY_NAME,
            market_id=signal.market.id,
            token_id=token_id,
            side=f"BUY_{signal.side}",
            size=size,
            price=buy_price,
            edge=signal.edge,
            reason=signal.reasoning,
        )

        if paper:
            # Paper simulation (same pattern as btc_latency)
            # Model decay: Betfair odds are ~75% informative, 25% noise
            model_decay = 0.75
            sim_prob = signal.fair_prob * model_decay + 0.5 * (1 - model_decay)
            if signal.side == "NO":
                sim_prob = 1.0 - sim_prob
            won = random.random() < sim_prob

            # Slippage: 1% for sport markets (wider spreads than crypto)
            slippage = 0.99
            if won:
                pnl = size * ((1.0 / buy_price) - 1.0) * slippage
            else:
                pnl = -size * slippage
            # Sport markets = fee-free
            fee = 0.0

            logger.info(
                f"[PAPER] SPORT-LATENCY: {signal.mode.upper()} "
                f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                f"fair_P={signal.fair_prob:.3f} edge={signal.edge:.4f} "
                f"conf={signal.confidence:.2f} | "
                f"{'WIN' if won else 'LOSS'} pnl=${pnl:+.2f} | "
                f"{signal.event_signal.event_name}"
            )

            self.risk.open_trade(trade)
            self.risk.close_trade(token_id, won=won, pnl=pnl)

            self._daily_pnl += pnl
            self._pnl_tracker[signal.market.id] = {
                "time": now,
                "mode": signal.mode,
                "event_type": signal.event_signal.event_type,
                "event_name": signal.event_signal.event_name,
                "side": signal.side,
                "price": buy_price,
                "size": size,
                "fair_prob": signal.fair_prob,
                "edge": signal.edge,
                "confidence": signal.confidence,
                "kelly": signal.kelly_fraction,
                "won": won,
                "pnl": pnl,
                "shift": signal.event_signal.implied_prob_shift,
            }
        else:
            # Live: aggressive market order for speed (latency is critical)
            result = self.api.smart_buy(
                token_id, size,
                target_price=min(buy_price + 0.03, 0.95),
                fallback_market=True,
            )
            if result:
                if isinstance(result, dict) and result.get("_fill_price"):
                    trade.price = result["_fill_price"]
                self.risk.open_trade(trade)
                self._open_positions += 1
                logger.info(
                    f"[LIVE] SPORT-LATENCY: {signal.mode.upper()} "
                    f"BUY {signal.side} ${size:.0f} @{buy_price:.3f} | "
                    f"fair_P={signal.fair_prob:.3f} edge={signal.edge:.4f} | "
                    f"{signal.event_signal.event_name}"
                )
            else:
                logger.warning(
                    f"[SPORT-LATENCY] Ordine fallito: {signal.market.id}"
                )
                return False

        self._recently_traded[event_key] = now
        self._trades_executed += 1
        return True

    # ══════════════════════════════════════════════════════════════
    #  Stats
    # ══════════════════════════════════════════════════════════════

    @property
    def stats(self) -> dict:
        """Strategy statistics for logging."""
        trades = list(self._pnl_tracker.values())
        if not trades:
            return {
                "trades": 0, "pnl": 0, "wr": 0,
                "avg_edge": 0, "modes": {},
            }

        total = len(trades)
        wins = sum(1 for t in trades if t.get("won"))
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        avg_edge = sum(t.get("edge", 0) for t in trades) / total

        # Breakdown by mode
        from collections import Counter
        mode_counts = Counter(t.get("mode") for t in trades)

        return {
            "trades": total,
            "wins": wins,
            "wr": wins / total if total > 0 else 0,
            "pnl": total_pnl,
            "avg_edge": avg_edge,
            "daily_pnl": self._daily_pnl,
            "modes": dict(mode_counts),
        }
