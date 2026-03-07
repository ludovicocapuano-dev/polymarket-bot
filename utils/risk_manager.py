"""
Risk Manager centralizzato per tutte le strategie.
Gestisce limiti di perdita, Kelly sizing, e circuit breaker.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import RiskConfig

logger = logging.getLogger(__name__)


@dataclass
class TripleBarrier:
    """Barriere di uscita per strategia (v7.2)."""
    take_profit: float      # es. +8% → vendi per profit
    stop_loss: float        # es. -12% → stop loss
    max_holding_hours: float  # barriera temporale


STRATEGY_BARRIERS: dict[str, TripleBarrier] = {
    "arb_gabagool":   TripleBarrier(0.03, 0.02, 24),    # TP/SL stretto, exit veloce
    "high_prob_bond": TripleBarrier(0.06, 0.03, 336),    # TP basso, SL stretto, 14gg
    "weather":        TripleBarrier(0.25, 0.08, 48),     # v10.6: TP da 12%→25% — hold to resolution su binary markets
    "arbitrage":      TripleBarrier(0.03, 0.02, 24),     # Come gabagool
    "data_driven":    TripleBarrier(0.10, 0.08, 72),     # Moderato, 3gg
    "event_driven":   TripleBarrier(0.12, 0.10, 36),     # Margini ampi, 1.5gg
    "whale_copy":     TripleBarrier(0.10, 0.08, 48),     # Segui il whale, 2gg
}

DEFAULT_BARRIER = TripleBarrier(0.10, 0.10, 48)


@dataclass
class Trade:
    timestamp: float
    strategy: str
    market_id: str
    token_id: str
    side: str  # BUY_YES, BUY_NO
    size: float
    price: float
    edge: float
    result: str = "OPEN"  # OPEN, WIN, LOSS
    pnl: float = 0.0
    reason: str = ""
    sell_failures: int = 0  # v5.9.9: track failed sell attempts


class RiskManager:
    """Risk manager condiviso tra tutte le strategie."""

    def __init__(self, config: RiskConfig):
        self.config = config
        self.capital = config.total_capital
        self.trades: list[Trade] = []
        self.open_trades: list[Trade] = []
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._global_halted = False
        self._global_halt_reason = ""
        self._session_start = time.time()

        # Budget per strategia
        self._strategy_budgets: dict[str, float] = {}
        self._strategy_pnl: dict[str, float] = {}

        # Halt e loss tracker PER STRATEGIA (non piu' globale)
        self._strategy_halted: dict[str, bool] = {}
        self._strategy_halt_reason: dict[str, str] = {}
        self._strategy_consecutive_losses: dict[str, int] = {}

        # v8.0: Stop-loss cooldown — blocca ri-acquisto sullo stesso mercato
        # dopo uno stop loss per evitare loop distruttivi (bond loop bug)
        self._stop_loss_cooldown: dict[str, float] = {}  # market_id → timestamp
        self.STOP_LOSS_COOLDOWN_HOURS = 4.0  # 4 ore di cooldown post stop-loss

        # v9.0: Correlation monitor (iniettato da bot.py)
        self.correlation_monitor = None
        # v9.2.1: WS feed per flash move protection (iniettato da bot.py)
        self.ws_feed = None
        # v9.2.1: VPIN monitor per toxic flow detection (iniettato da bot.py)
        self.vpin_monitor = None
        # v9.0: Database (iniettato da bot.py)
        self.db = None
        # v10.0: Empirical Kelly (iniettato da bot.py)
        self.empirical_kelly = None
        # v11.0: Drift detector (iniettato da bot.py) per dynamic σ
        self.drift_detector = None

    @property
    def total_exposed(self) -> float:
        """Capitale totale bloccato in posizioni aperte."""
        return sum(t.size for t in self.open_trades)

    @property
    def available_capital(self) -> float:
        """Capitale disponibile per nuovi trade."""
        return self.capital - self.total_exposed

    def set_strategy_budget(self, name: str, budget: float):
        self._strategy_budgets[name] = budget
        self._strategy_pnl.setdefault(name, 0.0)
        self._strategy_halted.setdefault(name, False)
        self._strategy_halt_reason.setdefault(name, "")
        self._strategy_consecutive_losses.setdefault(name, 0)

    def can_trade(
        self, strategy: str, size: float,
        price: float = 0.0, side: str = "",
        market_id: str = "",
    ) -> tuple[bool, str]:
        """
        Verifica se un trade e' consentito.

        v7.0: Aggiunto anti-hedging (blocca posizioni opposte sullo stesso mercato)
              e exposure limit (max 15% capitale per mercato).
        """
        # Halt globale: solo per perdita giornaliera complessiva
        if self._global_halted:
            return False, f"HALT GLOBALE: {self._global_halt_reason}"

        if self._daily_pnl <= -self.config.max_daily_loss:
            self._halt_global(f"Perdita giornaliera: ${abs(self._daily_pnl):.2f}")
            return False, "Limite perdita giornaliera superato"

        # Halt per strategia: loss consecutive isolate
        if self._strategy_halted.get(strategy, False):
            return False, f"HALT {strategy}: {self._strategy_halt_reason.get(strategy, '')}"

        strat_losses = self._strategy_consecutive_losses.get(strategy, 0)
        if strat_losses >= self.config.max_consecutive_losses:
            self._halt_strategy(strategy, f"{strat_losses} loss consecutive")
            return False, f"Troppe perdite consecutive per {strategy}"

        if len(self.open_trades) >= self.config.max_open_positions:
            return False, f"Max posizioni aperte ({self.config.max_open_positions})"

        # v8.1: Reserve floor — mantieni almeno X% del capitale liquido
        reserve_floor = self.config.total_capital * (self.config.reserve_floor_pct / 100.0)
        available = self.available_capital
        if available - size < reserve_floor:
            return False, (
                f"Reserve floor: ${available:.2f} disponibile, "
                f"dopo trade ${size:.2f} resterebbe ${available - size:.2f} "
                f"< floor ${reserve_floor:.2f} ({self.config.reserve_floor_pct:.0f}%)"
            )

        budget = self._strategy_budgets.get(strategy, 0)
        spent = self._strategy_pnl.get(strategy, 0)
        if spent <= -budget * 0.5:
            return False, f"Budget strategia {strategy} esaurito"

        max_pct = self.capital * (self.config.max_bet_percent / 100)
        if size > min(self.config.max_bet_size, max_pct):
            return False, f"Size ${size:.2f} supera limiti"

        if size < 1.0:
            return False, "Size < $1"

        # v5.9.8: Longshot filter (Becker 2026: YES < 20¢ returns -43¢/$1)
        # v10.7: Esenzione weather — forecast ensemble dà edge informativo reale,
        # e l'algoritmo EV filtra già i trade a basso valore atteso.
        # Miami 78-79°F: YES@$0.14 EV=+0.42 payoff=5.9x → NON bloccare.
        if side and "YES" in side.upper() and 0 < price < 0.20 and strategy != "weather":
            return False, f"Longshot filter: YES @${price:.2f} < $0.20"

        # v5.9.8: NO-bias filter (Becker 2026: NO beats YES at 69/99 price levels)
        # Block BUY_YES when price > $0.80 (risk/reward terrible: risk $0.80 for $0.20)
        # Prefer BUY_NO in these situations instead
        # v7.5: Esenzione per high_prob_bond (compra YES near-certain by design)
        if side and "YES" in side.upper() and price > 0.80 and strategy not in ("high_prob_bond", "weather"):
            return False, f"NO-bias filter: YES @${price:.2f} > $0.80 (prefer NO side)"

        # v8.0: Stop-loss cooldown — blocca ri-acquisto dopo stop loss
        if market_id and market_id in self._stop_loss_cooldown:
            sl_time = self._stop_loss_cooldown[market_id]
            hours_since = (time.time() - sl_time) / 3600
            if hours_since < self.STOP_LOSS_COOLDOWN_HOURS:
                remaining = self.STOP_LOSS_COOLDOWN_HOURS - hours_since
                return False, (
                    f"Stop-loss cooldown: mercato {market_id[:12]} "
                    f"stop-lossato {hours_since:.1f}h fa, "
                    f"riprova tra {remaining:.1f}h"
                )
            else:
                # Cooldown scaduto, rimuovi
                del self._stop_loss_cooldown[market_id]

        # v7.0: Anti-hedging — blocca posizioni opposte sullo stesso mercato
        # v9.2: Anti-stacking — blocca re-entry stessa direzione sullo stesso mercato
        if market_id and side:
            side_upper = side.upper()
            for t in self.open_trades:
                if t.market_id == market_id:
                    existing_side = t.side.upper()
                    # BUY_YES vs BUY_NO = hedging (spreco di capitale)
                    if ("YES" in side_upper and "NO" in existing_side) or \
                       ("NO" in side_upper and "YES" in existing_side):
                        return False, (
                            f"Anti-hedge: gia' {existing_side} su mercato {market_id[:12]} "
                            f"({t.strategy}), blocco {side_upper}"
                        )
                    # Stessa direzione = stacking (rischio concentrato)
                    if ("YES" in side_upper and "YES" in existing_side) or \
                       ("NO" in side_upper and "NO" in existing_side):
                        return False, (
                            f"Anti-stack: gia' {existing_side} ${t.size:.2f} su mercato "
                            f"{market_id[:12]} ({t.strategy}), blocco re-entry"
                        )

        # v7.0: Exposure limit — max 15% del capitale su un singolo mercato
        if market_id:
            MAX_MARKET_EXPOSURE = 0.15  # 15% del capitale
            existing_exposure = sum(
                t.size for t in self.open_trades if t.market_id == market_id
            )
            max_exposure = self.capital * MAX_MARKET_EXPOSURE
            if existing_exposure + size > max_exposure:
                return False, (
                    f"Exposure limit: mercato {market_id[:12]} "
                    f"gia' ${existing_exposure:.2f} + ${size:.2f} "
                    f"> max ${max_exposure:.2f} (15%)"
                )

        # v7.2: Edge gate — RIMOSSO v10.1: abs(0.5 - price) non è edge reale.
        # Kelly (b*p-q <= 0 → size=0) e il validator EV gate coprono già il caso.
        # if price > 0:
        #     rt_fee = 2 * price * (1.0 - price) * self.FEE_RATE + 0.005
        #     min_edge = 2.0 * rt_fee
        #     if 0 < price < 1 and size > 0:
        #         implied_edge = abs(0.5 - price)
        #         if implied_edge < min_edge and strategy not in ("arb_gabagool", "arbitrage"):
        #             return False, (...)


        # v7.3: Fees/Vol ratio gate (Bouchaud Theory of Financial Risk)
        # Blocca trade quando le fee round-trip superano il 30% della volatilità attesa.
        # Per prezzi estremi (vicini a 0 o 1), le fee mangiano qualsiasi profitto possibile.
        if price > 0 and price < 1:
            rt_fee = 2.0 * price * (1.0 - price) * self.FEE_RATE + self._estimate_spread_cost(price)
            expected_vol = price * (1.0 - price)  # vol binomiale del prezzo
            if expected_vol > 0 and rt_fee > 0.30 * expected_vol:
                return False, (
                    f"Fees/Vol gate: fee rt ${rt_fee:.4f} > 30% "
                    f"vol attesa ${expected_vol:.4f} (ratio={rt_fee/expected_vol:.1%})"
                )

        # v9.2.1: Flash Move Protection (Stoikov) — blocca trade su mercati
        # con price velocity > 5¢ in 60s (informed trading / manipolazione)
        if market_id and self.ws_feed:
            is_flash, flash_reason = self.ws_feed.is_flash_move(market_id)
            if is_flash:
                return False, f"Flash move protection: {flash_reason}"

        # v9.2.1: VPIN toxic flow — blocca trade su mercati con informed trading
        if market_id and self.vpin_monitor:
            is_toxic, vpin_reason = self.vpin_monitor.check_toxicity(market_id)
            if is_toxic:
                return False, f"VPIN toxic flow: {vpin_reason}"

        # v9.0: Correlation monitor — max 40% capitale per tema
        if self.correlation_monitor and market_id:
            theme = self.correlation_monitor.classify_theme(market_id)
            allowed, reason = self.correlation_monitor.check_correlation(
                market_id, theme, size
            )
            if not allowed:
                return False, reason

        return True, "OK"

    # ── Dynamic Kelly fractions per strategia (v5.3) ────────────
    # Dal modello matematico:
    #   - Weather same-day: 1/3 Kelly (previsioni accurate 85%+)
    #   - Weather multi-day: 1/4 Kelly
    #   - Arb/Gabagool: 1/2 Kelly (profitto quasi-garantito)
    #   - Crypto 5-min: 1/6 Kelly (alta volatilita')
    #   - Event-driven: 1/6 Kelly (incerto)
    #   - Data-driven: 1/5 Kelly
    KELLY_FRACTIONS: dict[str, float] = {
        "arb_gabagool":       0.50,   # 1/2 Kelly — profitto quasi-garantito
        "high_prob_bond":     0.15,   # v10.5: da 0.40 — rischio asimmetrico ($20 per $1)
        "arbitrage":          0.40,   # 2/5 Kelly — arb strutturale
        "weather":            0.35,   # v10.5: da 0.30 — unica profittevole, fee-free
        "data_driven":        0.12,   # v10.5: da 0.20 — edge inflato, WR 44%
        "event_driven":       0.20,   # v10.5: da 0.18 — Glint.trade + NLP
        "whale_copy":         0.12,   # ~1/8 Kelly — edge indiretto, dipende dal whale
        "crypto_5min":        0.16,   # 1/6 — volatile, cautela (DISABILITATO v7.0)
        "resolution_sniper":  0.40,   # v10.8: quasi risk-free (UMA proposta + scadenza)
    }

    # Fee rate Polymarket (taker). Maker = 0.
    FEE_RATE = 0.0625

    @staticmethod
    def _estimate_spread_cost(price: float) -> float:
        """
        v10.1: Spread cost stimato per exit taker, basato su pmxt orderbook data.
        Dati: 500K+ price_change events, 500 mercati (Feb 2026).
        Spread = costo di attraversamento del book in uscita (exit taker).
        """
        if price >= 0.93:
            return 0.005   # bond zone: spread stretto, exit cost ~metà di 0.010
        elif price >= 0.80:
            return 0.010   # high: spread moderato
        elif price >= 0.20:
            return 0.020   # mid range (weather/event): spread ampio
        else:
            return 0.010   # longshot: spread moderato

    def kelly_size(
        self,
        win_prob: float,
        price: float,
        strategy: str,
        is_maker: bool = True,
        days_ahead: int | None = None,
    ) -> float:
        """
        Kelly Criterion fee-adjusted e dinamico per strategia (v5.3).

        Miglioramenti:
        1. Fraction diversa per strategia (dal modello matematico)
        2. Fee-adjusted: taker paga fee, maker no
        3. Weather same-day boost: fraction piu' aggressiva
        """
        if price <= 0.001 or price >= 0.999 or win_prob <= 0:
            return 0.0

        # v7.3: Floor assoluto $30 (Vince + Brown)
        # Sotto $30 di capitale il bot non ha abbastanza bankroll
        # per recuperare da qualsiasi drawdown — meglio fermarsi
        FLOOR_CAPITAL = 30.0
        if self.capital < FLOOR_CAPITAL:
            logger.warning(
                f"[FLOOR] Capitale ${self.capital:.2f} < ${FLOOR_CAPITAL:.0f} "
                "— trading sospeso (bankroll insufficiente)"
            )
            return 0.0

        # Fee adjustment round-trip: entry fee + exit fee + spread stimato
        # fee = p * (1-p) * feeRate  (per side)
        if is_maker:
            entry_fee = 0.0
        else:
            entry_fee = price * (1.0 - price) * self.FEE_RATE
        exit_fee = price * (1.0 - price) * self.FEE_RATE  # exit sempre taker
        spread_cost = self._estimate_spread_cost(price)  # v10.1: pmxt data-driven
        total_cost = entry_fee + exit_fee + spread_cost

        # Payoff netto dopo fee round-trip
        b = (1.0 / price) - 1.0 - total_cost / price
        if b <= 0:
            return 0.0

        p, q = win_prob, 1.0 - win_prob
        kelly = (b * p - q) / b

        if kelly <= 0:
            return 0.0

        # Dynamic fraction per strategia
        base_frac = self.KELLY_FRACTIONS.get(strategy, self.config.kelly_fraction)

        # Weather same-day boost: +30% di fraction (previsioni accurate 85%+)
        if strategy == "weather" and days_ahead is not None and days_ahead == 0:
            base_frac = min(base_frac * 1.30, 0.40)  # Cap 2/5 Kelly

        # v10.0: Empirical Kelly — haircut data-driven basato su MC
        emp_factor = None
        if self.empirical_kelly is not None:
            emp_factor = self.empirical_kelly.get_adjustment_factor(strategy)
            if emp_factor is not None:
                # Blend 70% empirical + 30% statico (floor di sicurezza)
                base_frac = base_frac * (0.70 * emp_factor + 0.30)

        # v11.0: Dynamic uncertainty σ (Chu & Swartz 2024 + drift-adaptive)
        # Base σ per strategy, then scaled by drift_score from CUSUM detector.
        # drift_score ∈ [0, 1]: 0 = healthy, 1 = severe drift.
        # σ_dynamic = σ_base * (1 + drift_score) → max 2x base σ when drifting.
        uncertainty_sigmas = {
            "weather": 0.08,
            "resolution_sniper": 0.03,
            "event_driven": 0.12,
            "high_prob_bond": 0.05,
        }
        sigma = uncertainty_sigmas.get(strategy, 0.10)
        if strategy == "weather" and days_ahead is not None:
            sigma = 0.05 + days_ahead * 0.02

        # v11.0: Drift-adaptive σ scaling
        if self.drift_detector is not None:
            drift_score = self.drift_detector.get_drift_score(strategy)
            if drift_score > 0:
                sigma *= (1.0 + drift_score)
                logger.debug(
                    f"[DYNAMIC-σ] {strategy} drift={drift_score:.2f} "
                    f"→ σ={sigma:.3f}"
                )

        intrinsic_var = win_prob * (1 - win_prob)
        if intrinsic_var > 0:
            uncertainty_factor = max(0.3, 1.0 - (sigma ** 2) / intrinsic_var)
        else:
            uncertainty_factor = 0.3
        kelly *= uncertainty_factor

        # v7.3: Fat Tail Kelly Correction (Bouchaud + Taleb + Vince)
        # Applicato SOLO se Empirical Kelly non è attivo (altrimenti doppia correzione fat-tail)
        if emp_factor is None:
            KURTOSIS_PROXY = 4.0  # tipica per prediction markets
            kelly *= 1.0 / (1.0 + KURTOSIS_PROXY / 4.0)

        frac = kelly * base_frac

        # v7.3: Optimal f cap (Vince) — mai investire > 20% del budget in un trade
        # Anche con Kelly pieno e strategia aggressiva, l'empirical optimal f
        # per bankroll piccoli raramente supera 0.20
        frac = min(frac, 0.20)

        budget = self._strategy_budgets.get(strategy, self.capital * 0.2)
        size = frac * budget

        # v7.3: Grossman-Zhou cushion (Paleologo Elements of Quantitative Investing)
        # Allocazione proporzionale al "cuscino" sopra il floor.
        # Quando capitale → floor, size → 0 progressivamente.
        # Complementa il CPPI (che scala sul drawdown giornaliero).
        cushion = (self.capital - FLOOR_CAPITAL) / self.capital
        size *= cushion
        logger.debug(
            f"[CUSHION] capital=${self.capital:.2f} floor=${FLOOR_CAPITAL:.0f} "
            f"→ cushion={cushion:.2f}"
        )

        # v7.2: CPPI drawdown scaling (Isichenko + Chan)
        # Riduce size proporzionalmente al drawdown giornaliero
        if self._daily_pnl < 0:
            daily_loss_pct = abs(self._daily_pnl) / self.config.max_daily_loss
            # a 0% loss → scale 1.0, a 50% loss → scale 0.50, a 80% → scale 0.20
            cppi_scale = max(0.20, 1.0 - daily_loss_pct)
            size *= cppi_scale
            logger.debug(
                f"[CPPI] daily_pnl=${self._daily_pnl:+.2f} → scale={cppi_scale:.2f}"
            )

        # v7.2: Scaling per strategia in drawdown
        # Se una strategia ha perso >30% del suo budget, ridurre Kelly del 50%
        strategy_pnl = self._strategy_pnl.get(strategy, 0)
        if strategy_pnl < -budget * 0.30:
            size *= 0.50
            logger.debug(
                f"[CPPI] {strategy} pnl=${strategy_pnl:+.2f} "
                f"< -30% budget (${-budget * 0.30:.2f}) → size dimezzata"
            )

        # v7.3: Volatility Targeting (Ilmanen + Bouchaud MAD)
        # Size inversamente proporzionale alla volatilità recente della strategia.
        # Se la strategia è stata molto volatile, riduce; se calma, può aumentare (max 1.5x)
        vol = self._recent_volatility(strategy)
        if vol > 0 and budget > 0:
            target_vol = budget * 0.02  # 2% del budget come target vol
            vol_scale = min(1.5, max(0.30, target_vol / vol))
            size *= vol_scale
            logger.debug(
                f"[VOL_TARGET] {strategy} vol=${vol:.4f} target=${target_vol:.4f} "
                f"→ scale={vol_scale:.2f}"
            )

        # v7.0: Kelly-proporzionale con floor dinamico basato su budget.
        # RIMOSSO il floor fisso PREFERRED_MIN=$50 che annullava il Kelly
        # forzando TUTTI i trade alla stessa size indipendentemente dall'edge.
        # Ora: floor = max(5, budget * 2%) — scala col capitale disponibile.
        HARD_MIN = 5.0  # Absolute floor (execution minimum Polymarket)

        # v10.6: Floor dinamico CONDIZIONALE — non forzare trade con Kelly troppo debole.
        # Se Kelly produce un size < 50% del floor, il trade è troppo marginale
        # e forzare una size minima crea EV negativa (vedi weather PnL analysis).
        dynamic_min = max(HARD_MIN, budget * 0.02)
        if size < dynamic_min:
            if size >= dynamic_min * 0.50:
                size = dynamic_min  # Kelly ragionevole, arrotonda al minimo
            else:
                size = 0.0  # Kelly troppo debole, non forzare

        max_pct = self.capital * (self.config.max_bet_percent / 100)
        size = min(size, self.config.max_bet_size, max_pct)

        return round(size, 2) if size >= HARD_MIN else 0.0

    def _recent_volatility(self, strategy: str, lookback: int = 20) -> float:
        """
        v10.2: GARCH(1,1) con exponential weighting (MIT 18.S096 Lectures 7+9).

        Sostituisce MAD con GARCH(1,1):
          sigma_t^2 = omega + alpha * epsilon_{t-1}^2 + beta * sigma_{t-1}^2

        Exponential weighting (lambda=0.94, RiskMetrics/Abbott) per la media:
        pesa dati recenti più delle osservazioni vecchie.

        Vantaggi vs MAD:
        1. Cattura volatility clustering (dopo un loss grande, vol resta alta)
        2. Forward-looking (predice vol prossimo periodo, non solo misura passato)
        3. Reattivo a regime change (exp weighting sulla media)
        """
        recent = [
            t.pnl for t in self.trades
            if t.strategy == strategy and t.result in ("WIN", "LOSS")
        ]
        recent = recent[-lookback:]
        if len(recent) < 3:
            return 0.0  # non abbastanza dati per stimare

        # Exponential weighting per media (lambda=0.94, MIT Lecture 7: Abbott)
        EW_LAMBDA = 0.94
        weights = [EW_LAMBDA ** i for i in range(len(recent) - 1, -1, -1)]
        w_sum = sum(weights)
        mean_pnl = sum(w * x for w, x in zip(weights, recent)) / w_sum

        # Residui (demeaned PnL)
        residuals = [x - mean_pnl for x in recent]

        # Varianza incondizionata (exp-weighted) come inizializzazione
        var_unconditional = sum(w * r ** 2 for w, r in zip(weights, residuals)) / w_sum

        # GARCH(1,1) parameters (MIT Lecture 9: Kempthorne)
        # alpha + beta < 1 per stazionarietà
        ALPHA = 0.06   # reattività a shock (peso dell'innovazione)
        BETA = 0.93    # persistenza (peso della varianza passata)
        # omega calibrato per E[sigma^2] = var_unconditional
        OMEGA = var_unconditional * (1.0 - ALPHA - BETA)

        # GARCH(1,1) recursion: sigma_t^2 = omega + alpha*e_{t-1}^2 + beta*sigma_{t-1}^2
        sigma_sq = var_unconditional  # init alla varianza incondizionata
        for r in residuals:
            sigma_sq = OMEGA + ALPHA * r ** 2 + BETA * sigma_sq

        return max(sigma_sq ** 0.5, 0.0)

    def open_trade(self, trade: Trade):
        self.trades.append(trade)
        self.open_trades.append(trade)
        self._save_open_positions()
        logger.info(
            f"[{trade.strategy}] APERTO {trade.side} ${trade.size:.2f} "
            f"@{trade.price:.4f} edge={trade.edge:.4f}"
        )

    def close_trade(self, token_id: str, won: bool, pnl: float):
        for i, t in enumerate(self.open_trades):
            if t.token_id == token_id:
                t.result = "WIN" if won else "LOSS"
                t.pnl = pnl
                self.open_trades.pop(i)

                self._daily_pnl += pnl
                self.capital += pnl
                self._strategy_pnl[t.strategy] = self._strategy_pnl.get(t.strategy, 0) + pnl

                # Track consecutive losses PER STRATEGIA
                if won:
                    self._consecutive_losses = 0
                    self._strategy_consecutive_losses[t.strategy] = 0
                else:
                    self._consecutive_losses += 1
                    self._strategy_consecutive_losses[t.strategy] = \
                        self._strategy_consecutive_losses.get(t.strategy, 0) + 1

                self._save_open_positions()
                logger.info(
                    f"[{t.strategy}] {'VINTO' if won else 'PERSO'} "
                    f"PnL=${pnl:+.2f} | Giorn=${self._daily_pnl:+.2f} | "
                    f"Cap=${self.capital:.2f}"
                )
                break

    def register_stop_loss(self, market_id: str):
        """v8.0: Registra un mercato come stop-lossato per bloccare ri-acquisto."""
        self._stop_loss_cooldown[market_id] = time.time()
        logger.info(
            f"[STOP-LOSS-COOLDOWN] Mercato {market_id[:12]} bloccato "
            f"per {self.STOP_LOSS_COOLDOWN_HOURS:.0f}h"
        )

    def check_barrier(self, trade: Trade, current_bid: float) -> str:
        """
        v7.2: Triple-Barrier Exit System.
        Ritorna 'HOLD', 'TAKE_PROFIT', 'STOP_LOSS', 'TIME_EXIT'.
        """
        barrier = STRATEGY_BARRIERS.get(trade.strategy, DEFAULT_BARRIER)
        pnl_pct = (current_bid - trade.price) / trade.price if trade.price > 0 else 0
        age_hours = (time.time() - trade.timestamp) / 3600

        if pnl_pct >= barrier.take_profit:
            return "TAKE_PROFIT"
        if pnl_pct <= -barrier.stop_loss:
            return "STOP_LOSS"
        if age_hours >= barrier.max_holding_hours:
            return "TIME_EXIT"
        return "HOLD"

    def _halt_global(self, reason: str):
        """Halt globale: ferma TUTTE le strategie."""
        self._global_halted = True
        self._global_halt_reason = reason
        logger.warning(f"HALT GLOBALE: {reason}")

    def _halt_strategy(self, strategy: str, reason: str):
        """Halt per singola strategia: le altre continuano."""
        self._strategy_halted[strategy] = True
        self._strategy_halt_reason[strategy] = reason
        logger.warning(f"HALT {strategy}: {reason}")

    def resume(self, strategy: str | None = None):
        """Riprendi una strategia specifica o tutte."""
        if strategy:
            self._strategy_halted[strategy] = False
            self._strategy_halt_reason[strategy] = ""
            self._strategy_consecutive_losses[strategy] = 0
            logger.info(f"Strategia {strategy} ripresa")
        else:
            self._global_halted = False
            self._global_halt_reason = ""
            self._consecutive_losses = 0
            for k in self._strategy_halted:
                self._strategy_halted[k] = False
                self._strategy_halt_reason[k] = ""
                self._strategy_consecutive_losses[k] = 0
            logger.info("Tutte le strategie riprese")

    def reset_daily(self):
        old_pnl = self._daily_pnl
        self._daily_pnl = 0.0
        for k in self._strategy_pnl:
            self._strategy_pnl[k] = 0.0
        # Riprendi halt globale se era per perdita giornaliera
        if self._global_halted and "giornalier" in self._global_halt_reason.lower():
            self.resume()
        # Riprendi anche le strategie haltate per loss consecutive
        for k in list(self._strategy_halted.keys()):
            if self._strategy_halted.get(k):
                self.resume(k)
        logger.info(f"Reset giornaliero. PnL chiuso: ${old_pnl:+.2f}")

    @property
    def status(self) -> dict:
        wins = sum(1 for t in self.trades if t.result == "WIN")
        losses = sum(1 for t in self.trades if t.result == "LOSS")
        total = wins + losses

        # Stato halt: globale + per-strategia
        any_halted = self._global_halted or any(self._strategy_halted.values())
        halt_reasons = []
        if self._global_halted:
            halt_reasons.append(f"GLOBALE: {self._global_halt_reason}")
        for k, v in self._strategy_halted.items():
            if v:
                halt_reasons.append(f"{k}: {self._strategy_halt_reason.get(k, '')}")

        return {
            "capital": round(self.capital, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "total_trades": len(self.trades),
            "open": len(self.open_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total else 0.0,
            "halted": any_halted,
            "halt_reason": " | ".join(halt_reasons) if halt_reasons else "",
            "exposed": round(self.total_exposed, 2),
            "available": round(self.available_capital, 2),
            "reserve_floor": round(self.config.total_capital * (self.config.reserve_floor_pct / 100.0), 2),
            "strategy_pnl": {k: round(v, 2) for k, v in self._strategy_pnl.items()},
            "strategy_halted": dict(self._strategy_halted),
        }

    # ── Persistenza posizioni aperte ────────────────────────────
    _OPEN_POS_FILE = "logs/open_positions.json"

    def _save_open_positions(self):
        """Salva posizioni aperte su disco — sopravvive ai restart."""
        try:
            data = []
            for t in self.open_trades:
                data.append({
                    "timestamp": t.timestamp,
                    "strategy": t.strategy,
                    "market_id": t.market_id,
                    "token_id": t.token_id,
                    "side": t.side,
                    "size": t.size,
                    "price": t.price,
                    "edge": t.edge,
                    "reason": t.reason,
                })
            os.makedirs(os.path.dirname(self._OPEN_POS_FILE), exist_ok=True)
            with open(self._OPEN_POS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"Errore salvataggio posizioni: {e}")

    def load_open_positions(self, max_age_hours: float = 48.0):
        """
        Ricarica posizioni aperte dal disco (chiamato all'avvio del bot).

        v5.3: Filtra posizioni piu' vecchie di max_age_hours.
        Posizioni stale (paper o mercati gia' risolti) non vengono caricate.
        """
        try:
            if not os.path.exists(self._OPEN_POS_FILE):
                return
            with open(self._OPEN_POS_FILE, "r") as f:
                data = json.load(f)
            if not data:
                return
            now = time.time()
            cutoff = now - max_age_hours * 3600
            loaded = 0
            skipped_stale = 0
            for d in data:
                ts = d.get("timestamp", 0)
                # Scarta posizioni troppo vecchie
                if ts < cutoff:
                    skipped_stale += 1
                    continue

                raw_price = d["price"]
                reason = d.get("reason", "")

                # v10.6: Zombie sanitizer — fix entry prices corrupted by fill_price bug.
                # If reason contains "YES@X.XXXX", cross-reference with recorded price.
                # A price < 30% of the stated entry is a zombie (other trader's fill).
                import re as _re
                _m = _re.search(r"YES@(\d+\.\d+)", reason)
                if _m:
                    stated_price = float(_m.group(1))
                    if raw_price < stated_price * 0.30:
                        logger.warning(
                            f"[SANITIZER] Zombie detectata: market={d['market_id']} "
                            f"price={raw_price:.4f} vs stated={stated_price:.4f} — corretto"
                        )
                        raw_price = stated_price

                trade = Trade(
                    timestamp=ts,
                    strategy=d["strategy"],
                    market_id=d["market_id"],
                    token_id=d["token_id"],
                    side=d["side"],
                    size=d["size"],
                    price=raw_price,
                    edge=d["edge"],
                    result="OPEN",
                    reason=reason,
                )
                # Evita duplicati (se gia' presente in open_trades)
                already = any(
                    t.market_id == trade.market_id and t.token_id == trade.token_id
                    for t in self.open_trades
                )
                if not already:
                    self.trades.append(trade)
                    self.open_trades.append(trade)
                    loaded += 1
            if loaded or skipped_stale:
                logger.info(
                    f"[PERSISTENCE] Ricaricate {loaded} posizioni aperte "
                    f"(scartate {skipped_stale} stale >{max_age_hours:.0f}h)"
                )
            # Risalva per rimuovere le stale dal file
            if skipped_stale:
                self._save_open_positions()
        except Exception as e:
            logger.warning(f"[PERSISTENCE] Errore caricamento posizioni: {e}")

    def purge_stale_positions(self, max_age_hours: float = 48.0) -> int:
        """Rimuovi posizioni aperte piu' vecchie di max_age_hours."""
        cutoff = time.time() - max_age_hours * 3600
        stale = [t for t in self.open_trades if t.timestamp < cutoff]
        for t in stale:
            self.open_trades.remove(t)
            t.result = "STALE"
        if stale:
            self._save_open_positions()
            logger.info(f"[PERSISTENCE] Purgate {len(stale)} posizioni stale")
        return len(stale)

    def save_trades(self, path: str = "logs/trades.json"):
        """Salva lo storico trade su file (e su DB se disponibile)."""
        data = []
        for t in self.trades:
            data.append({
                "time": datetime.fromtimestamp(t.timestamp, tz=timezone.utc).isoformat(),
                "strategy": t.strategy,
                "market": t.market_id,
                "side": t.side,
                "size": t.size,
                "price": t.price,
                "edge": t.edge,
                "result": t.result,
                "pnl": t.pnl,
                "reason": t.reason,
            })
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

        # v9.0: Sync chiusure su DB (solo trade appena chiusi)
        if self.db and hasattr(self.db, 'available') and self.db.available:
            for t in self.trades:
                if t.result in ("WIN", "LOSS"):
                    try:
                        self.db.update_trade_result(
                            market_id=t.market_id,
                            token_id=t.token_id,
                            result=t.result,
                            pnl=t.pnl,
                        )
                    except Exception:
                        pass  # graceful
