"""
Strategia: Resolution Sniping + High-Probability Bonds (v5.9.2)
================================================================
Due approcci combinati per profitto quasi-certo:

1. RESOLUTION SNIPING: Compra quando l'esito è già proposto su UMA
   ma il prezzo Polymarket non ha ancora reagito completamente.
   Edge tipico: 5-15%. Win rate: ~95% (rischio solo su dispute).

2. HIGH-PROBABILITY BONDS: Compra token a >0.92 quando la risoluzione
   è imminente e l'esito è praticamente certo.
   Edge tipico: 2-8%. Win rate: ~90%.
   Ispirato dai top trader Polymarket: 90% degli ordini >$10k sono
   a prezzi >0.95 ("bonding" su quasi-certezze).

Fonti:
- Analisi 95M transazioni on-chain (Polymarket Six Profit Models)
- Top 668 wallet con >$1M profitti = 71% dei guadagni totali
- gabagool22 strategy: arbitraggio meccanico, non direzionale
"""

import logging
import time
from dataclasses import dataclass

from utils.polymarket_api import Market, PolymarketAPI
from utils.risk_manager import RiskManager, Trade
from utils.uma_monitor import UmaMonitor, ResolutionProposal

logger = logging.getLogger(__name__)

STRATEGY_NAME = "resolution_sniper"


@dataclass
class SniperSignal:
    """Segnale da resolution sniping o high-prob bonds."""
    market: Market
    side: str         # "YES" o "NO"
    edge: float
    confidence: float
    signal_type: str  # "resolution_snipe" o "high_prob_bond"
    reasoning: str


class ResolutionSniperStrategy:
    """
    Combina resolution sniping + high-probability bonds.

    Non predice nulla. Compra solo quando:
    - L'esito è già noto (proposto o quasi-certo) ma il prezzo non riflette
    - Il margine dopo fee copre il rischio residuo (dispute)
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        uma_monitor: UmaMonitor | None = None,
        min_edge: float = 0.03,
    ):
        self.api = api
        self.risk = risk
        self.uma = uma_monitor
        self.min_edge = min_edge
        self._trades_executed = 0
        self._recently_traded: dict[str, float] = {}

    async def scan(
        self, shared_markets: list[Market] | None = None
    ) -> list[SniperSignal]:
        """Scansiona per opportunità di sniping e bonds."""
        signals = []
        markets = shared_markets or self.api.fetch_markets(limit=400)

        if not markets:
            return []

        # 1. Resolution sniping (se UMA monitor attivo)
        if self.uma:
            snipe_signals = self._check_resolution_snipes(markets)
            signals.extend(snipe_signals)

        # 2. High-probability bonds
        # v5.9.4: HIGH_PROB_BOND DISABILITATO — 0% win rate, prezzo != probabilita'
        # bond_signals = self._check_high_prob_bonds(markets)
        # signals.extend(bond_signals)

        signals.sort(key=lambda s: s.edge * s.confidence, reverse=True)

        if signals:
            by_type = {}
            for s in signals:
                by_type[s.signal_type] = by_type.get(s.signal_type, 0) + 1
            type_str = " ".join(f"{k}:{v}" for k, v in by_type.items())
            logger.info(
                f"[SNIPER] Scan {len(markets)} mercati → "
                f"{len(signals)} segnali [{type_str}] "
                f"migliore: edge={signals[0].edge:.3f} {signals[0].signal_type}"
            )
        else:
            logger.info(f"[SNIPER] Scan {len(markets)} mercati → 0 segnali")

        return signals

    def _check_resolution_snipes(
        self, markets: list[Market]
    ) -> list[SniperSignal]:
        """
        Controlla se ci sono mercati con proposta UMA dove il prezzo
        non ha ancora reagito.
        """
        signals = []

        if not self.uma:
            return signals

        opps = self.uma.get_opportunities(min_edge=self.min_edge)
        market_map = {str(m.id): m for m in markets}

        for prop in opps:
            if not prop.market_id or prop.market_id not in market_map:
                continue

            m = market_map[prop.market_id]

            # Cooldown v5.9.4: 24h
            if m.id in self._recently_traded:
                if time.time() - self._recently_traded[m.id] < 86400:
                    continue

            side = prop.proposed_outcome  # "YES" o "NO"
            edge = prop.best_edge

            # Confidence basata su quanto manca alla finalizzazione
            secs_left = prop.seconds_until_final
            if secs_left > 6000:  # > 100 min → ancora tempo per dispute
                confidence = 0.75
            elif secs_left > 3600:  # 60-100 min
                confidence = 0.82
            else:  # < 60 min → quasi finalizzata
                confidence = 0.90

            if edge > self.min_edge:
                signals.append(SniperSignal(
                    market=m,
                    side=side,
                    edge=edge,
                    confidence=confidence,
                    signal_type="resolution_snipe",
                    reasoning=(
                        f"UMA proposta: {side} | "
                        f"Price={prop.current_yes_price:.3f}/{prop.current_no_price:.3f} | "
                        f"Edge={edge:.3f} | "
                        f"Finalize in {secs_left/60:.0f}min"
                    ),
                ))

        return signals

    def _check_high_prob_bonds(
        self, markets: list[Market]
    ) -> list[SniperSignal]:
        """
        Cerca mercati dove un outcome è quasi certo (>0.92) e
        il token opposto costa pochissimo, ma c'è ancora margine.

        Strategia "bonding": comprare a 0.92-0.97 per 3-8% di profitto
        quando la risoluzione è imminente o l'evento è già accaduto.

        Filtri di sicurezza:
        - Solo mercati con end_date nel passato o prossime 24h
        - Solo se il prezzo è stabile (non in movimento rapido)
        - NO mercati controversi (politica divisiva, dispute UMA)
        """
        signals = []
        now = time.time()

        for m in markets:
            # Cooldown
            if m.id in self._recently_traded:
                if now - self._recently_traded[m.id] < 600:
                    continue

            p_yes = m.prices.get("yes", 0.5)
            p_no = m.prices.get("no", 0.5)

            # Cerchiamo mercati con forte consenso su un lato
            # ma dove c'è ancora margine di profitto
            best_side = None
            best_price = 0
            best_edge = 0

            if p_yes >= 0.92 and p_yes <= 0.97:
                # YES quasi certo → compra YES
                best_side = "YES"
                best_price = p_yes
                best_edge = 1.0 - p_yes  # profitto se YES vince
            elif p_no >= 0.92 and p_no <= 0.97:
                # NO quasi certo → compra NO
                best_side = "NO"
                best_price = p_no
                best_edge = 1.0 - p_no

            if not best_side or best_edge < self.min_edge:
                continue

            # Filtro liquidità: skip mercati con meno di $500 liquidity
            if m.liquidity < 500:
                continue

            # Calcola confidence basata sul prezzo
            # 0.92 → conf 0.80, 0.95 → conf 0.88, 0.97 → conf 0.92
            confidence = 0.60 + best_price * 0.33
            confidence = min(confidence, 0.95)

            signals.append(SniperSignal(
                market=m,
                side=best_side,
                edge=best_edge,
                confidence=confidence,
                signal_type="high_prob_bond",
                reasoning=(
                    f"Bond {best_side}@{best_price:.3f} | "
                    f"Edge={best_edge:.3f} | "
                    f"Liq=${m.liquidity:,.0f} | "
                    f"'{m.question[:40]}'"
                ),
            ))

        return signals

    async def execute(
        self, signal: SniperSignal, paper: bool = True
    ) -> bool:
        """Esegui un trade di sniping o bonding."""
        now = time.time()

        # Cooldown v5.9.4: 24h
        last = self._recently_traded.get(signal.market.id, 0)
        if now - last < 86400:
            logger.info(f"[SNIPER] Skip {signal.market.id[:8]}… cooldown {int(now-last)}s/86400s")
            return False

        # Anti-contraddizione
        for open_t in self.risk.open_trades:
            if open_t.market_id == signal.market.id:
                logger.info(f"[SNIPER] Skip {signal.market.id[:8]}… posizione gia' aperta")
                return False

        token_key = "yes" if signal.side == "YES" else "no"
        token_id = signal.market.tokens[token_key]
        price = signal.market.prices[token_key]

        # Size: per bonds, possiamo essere più aggressivi
        # perché la probabilità è alta
        if signal.signal_type == "resolution_snipe":
            # Sniping: edge alto ma rischio dispute → size moderata
            win_prob = signal.confidence
        else:
            # Bonding: la probabilita' reale e' price + edge (non solo price!)
            # Se price=0.92 e edge=0.08, win_prob=0.968 → Kelly ha un edge
            win_prob = min(price + signal.edge * 0.6, 0.99)

        size = self.risk.kelly_size(
            win_prob=win_prob,
            price=price,
            strategy=STRATEGY_NAME,
            is_maker=True,
        )

        if size == 0:
            logger.info(f"[SNIPER] Skip {signal.market.id[:8]}… kelly_size=0 (wp={win_prob:.2f} p={price:.2f})")
            return False

        # Per bonds, aumenta size (rischio bassissimo)
        if signal.signal_type == "high_prob_bond":
            size = min(size * 1.5, self.risk.config.max_bet_size)

        allowed, reason = self.risk.can_trade(STRATEGY_NAME, size, price=price, side=f"BUY_{signal.side}", market_id=signal.market.id)
        if not allowed:
            logger.info(f"[SNIPER] Trade bloccato: {reason}")
            return False

        trade = Trade(
            timestamp=now,
            strategy=STRATEGY_NAME,
            market_id=signal.market.id,
            token_id=token_id,
            side=f"BUY_{signal.side}",
            size=size,
            price=price,
            edge=signal.edge,
            reason=signal.reasoning,
        )

        if paper:
            import random
            logger.info(
                f"[PAPER] SNIPER-{signal.signal_type.upper()}: "
                f"BUY {signal.side} '{signal.market.question[:40]}' "
                f"${size:.2f} @{price:.4f} edge={signal.edge:.3f}"
            )
            self.risk.open_trade(trade)

            # Simulazione: win prob molto alta per sniping/bonding
            sim_win_prob = min(signal.confidence, 0.95)
            won = random.random() < sim_win_prob

            slippage = 0.95 + random.random() * 0.04
            if won:
                pnl = size * ((1.0 / price) - 1.0) * slippage
            else:
                pnl = -size * slippage

            self.risk.close_trade(token_id, won=won, pnl=pnl)
        else:
            result = self.api.smart_buy(
                token_id, size, target_price=price,
                timeout_sec=8.0, fallback_market=True,
            )
            if result:
                self.risk.open_trade(trade)

        self._recently_traded[signal.market.id] = now
        self._trades_executed += 1
        return True

    @property
    def stats(self) -> dict:
        return {
            "trades_executed": self._trades_executed,
            "uma_proposals": self.uma.active_proposals if self.uma else 0,
        }
