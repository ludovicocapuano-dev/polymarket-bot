"""
Execution Engine v11.1 — Ottimizzazione piazzamento ordini.

Strategie di esecuzione:
- LIMIT_MAKER: standard smart_buy per trade <= $30
- TWAP: tranche da $15 ogni 2s per trade > $30
- ALPHA_DECAY: front-loaded execution per segnali con alpha decadente (TQP Ch 21.2.3)
- SNIPER: aspetta spread stretto (futuro, non implementato in v1.0)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ExecutionStrategy(Enum):
    LIMIT_MAKER = "limit_maker"
    TWAP = "twap"
    ALPHA_DECAY = "alpha_decay"
    SNIPER = "sniper"


@dataclass
class ExecutionPlan:
    """Piano di esecuzione per un trade."""
    token_id: str
    total_size: float           # $ totali da piazzare
    target_price: float         # prezzo target
    strategy: ExecutionStrategy
    splits: int = 1             # numero tranche
    interval_sec: float = 2.0   # intervallo tra tranche
    max_slippage: float = 0.02  # max 2% slippage accettato
    decay_factor: float = 0.0   # front-loading factor per ALPHA_DECAY (0=uniform, 0.5=50% front-loaded)

    @property
    def tranche_size(self) -> float:
        return self.total_size / self.splits if self.splits > 0 else self.total_size


@dataclass
class ExecutionResult:
    """Risultato dell'esecuzione di un piano."""
    total_filled: float = 0.0       # $ totali riempiti
    fills: int = 0                  # numero fill
    avg_price: float = 0.0          # prezzo medio di riempimento
    slippage: float = 0.0           # slippage vs target
    duration_sec: float = 0.0       # tempo totale esecuzione
    partial: bool = False           # True se non completamente riempito

    def __str__(self) -> str:
        return (
            f"Exec[fills={self.fills} filled=${self.total_filled:.2f} "
            f"avg={self.avg_price:.4f} slippage={self.slippage:.4f} "
            f"{self.duration_sec:.1f}s {'PARTIAL' if self.partial else 'FULL'}]"
        )


class ExecutionAgent:
    """Ottimizza il piazzamento ordini basandosi sulla size."""

    TWAP_THRESHOLD = 30.0       # Trade > $30 usa TWAP
    TWAP_TRANCHE_SIZE = 15.0    # $15 per tranche
    TWAP_INTERVAL = 2.0         # 2 secondi tra tranche
    MAX_SLIPPAGE = 0.02         # 2% max slippage

    def __init__(self, api=None):
        self.api = api

    def plan_execution(self, token_id: str, size: float,
                       target_price: float) -> ExecutionPlan:
        """Crea un piano di esecuzione ottimale."""
        if size <= self.TWAP_THRESHOLD:
            # Trade piccoli: ordine singolo
            return ExecutionPlan(
                token_id=token_id,
                total_size=size,
                target_price=target_price,
                strategy=ExecutionStrategy.LIMIT_MAKER,
                splits=1,
                max_slippage=self.MAX_SLIPPAGE,
            )
        else:
            # Trade grandi: TWAP
            splits = max(2, int(size / self.TWAP_TRANCHE_SIZE))
            return ExecutionPlan(
                token_id=token_id,
                total_size=size,
                target_price=target_price,
                strategy=ExecutionStrategy.TWAP,
                splits=splits,
                interval_sec=self.TWAP_INTERVAL,
                max_slippage=self.MAX_SLIPPAGE,
            )

    async def execute_plan(self, plan: ExecutionPlan,
                           paper: bool = True) -> ExecutionResult:
        """
        Esegue un piano di esecuzione.

        - LIMIT_MAKER: singola chiamata smart_buy
        - TWAP: tranche sequenziali con intervallo
        """
        start = time.time()

        if plan.strategy == ExecutionStrategy.LIMIT_MAKER:
            result = await self._execute_limit(plan, paper)
        elif plan.strategy == ExecutionStrategy.TWAP:
            result = await self._execute_twap(plan, paper)
        elif plan.strategy == ExecutionStrategy.ALPHA_DECAY:
            result = await self._execute_alpha_decay(plan, paper)
        else:
            logger.warning(f"[EXEC] Strategia {plan.strategy} non implementata, fallback LIMIT")
            result = await self._execute_limit(plan, paper)

        result.duration_sec = time.time() - start

        logger.info(
            f"[EXEC] {plan.strategy.value} {result}"
        )
        return result

    async def _execute_limit(self, plan: ExecutionPlan,
                             paper: bool) -> ExecutionResult:
        """Esecuzione singolo ordine limit."""
        if paper:
            # Simulazione paper trading
            import random
            slippage = random.uniform(0, plan.max_slippage)
            fill_price = plan.target_price * (1 + slippage)
            return ExecutionResult(
                total_filled=plan.total_size,
                fills=1,
                avg_price=fill_price,
                slippage=slippage,
            )

        if not self.api:
            logger.error("[EXEC] API non disponibile")
            return ExecutionResult()

        try:
            shares = plan.total_size / plan.target_price if plan.target_price > 0 else 0
            result = await asyncio.to_thread(
                self.api.smart_buy,
                plan.token_id,
                shares,
                target_price=plan.target_price,
            )

            if result:
                fill_price = result.get("fill_price", plan.target_price)
                slippage = (fill_price - plan.target_price) / plan.target_price if plan.target_price > 0 else 0
                return ExecutionResult(
                    total_filled=plan.total_size,
                    fills=1,
                    avg_price=fill_price,
                    slippage=abs(slippage),
                )
            else:
                return ExecutionResult(partial=True)
        except Exception as e:
            logger.error(f"[EXEC] Errore limit: {e}")
            return ExecutionResult(partial=True)

    async def _execute_twap(self, plan: ExecutionPlan,
                            paper: bool) -> ExecutionResult:
        """Esecuzione TWAP — tranche sequenziali."""
        total_filled = 0.0
        fills = 0
        weighted_price_sum = 0.0

        for i in range(plan.splits):
            tranche_size = plan.tranche_size

            # Ultima tranche: aggiusta per resti
            remaining = plan.total_size - total_filled
            if remaining < tranche_size:
                tranche_size = remaining
            if tranche_size < 1.0:
                break

            if paper:
                import random
                slippage = random.uniform(0, plan.max_slippage)
                fill_price = plan.target_price * (1 + slippage)
                total_filled += tranche_size
                weighted_price_sum += fill_price * tranche_size
                fills += 1
            elif self.api:
                try:
                    # Controlla spread corrente prima di ogni tranche
                    book = await asyncio.to_thread(
                        self.api.get_order_book, plan.token_id
                    )
                    asks = book.get("asks", [])
                    if asks:
                        current_ask = float(asks[0]["price"])
                        slippage = (current_ask - plan.target_price) / plan.target_price if plan.target_price > 0 else 0

                        # Blocca se slippage troppo alto
                        if slippage > plan.max_slippage:
                            logger.warning(
                                f"[EXEC] TWAP tranche {i+1}/{plan.splits} "
                                f"slippage={slippage:.4f} > max={plan.max_slippage} — STOP"
                            )
                            break

                    shares = tranche_size / plan.target_price if plan.target_price > 0 else 0
                    result = await asyncio.to_thread(
                        self.api.smart_buy,
                        plan.token_id,
                        shares,
                        target_price=plan.target_price,
                    )

                    if result:
                        fill_price = result.get("fill_price", plan.target_price)
                        total_filled += tranche_size
                        weighted_price_sum += fill_price * tranche_size
                        fills += 1
                    else:
                        logger.warning(f"[EXEC] TWAP tranche {i+1} fill fallito")
                except Exception as e:
                    logger.warning(f"[EXEC] TWAP tranche {i+1} errore: {e}")

            # Attendi tra tranche (tranne l'ultima)
            if i < plan.splits - 1:
                await asyncio.sleep(plan.interval_sec)

        avg_price = weighted_price_sum / total_filled if total_filled > 0 else plan.target_price
        overall_slippage = abs(avg_price - plan.target_price) / plan.target_price if plan.target_price > 0 else 0

        return ExecutionResult(
            total_filled=total_filled,
            fills=fills,
            avg_price=avg_price,
            slippage=overall_slippage,
            partial=total_filled < plan.total_size * 0.95,
        )

    # ------------------------------------------------------------------
    # ALPHA_DECAY — front-loaded execution (TQP Ch 21.2.3)
    # ------------------------------------------------------------------

    @staticmethod
    def _alpha_decay_weights(n_tranches: int, decay_factor: float) -> list[float]:
        """
        Compute front-loaded tranche weights.

        Optimal schedule from Trades, Quotes and Prices (21.2.3):
            j*(t) = Q/T + alpha/G_0 * (T - 2t) / (2*T_alpha)

        Simplified discrete version:
            w_i = 1 + decay_factor * (1 - 2*i/(N-1))
        First tranche gets weight (1 + decay_factor), last gets (1 - decay_factor).
        Weights are normalized so they sum to N (i.e., average weight = 1).
        """
        if n_tranches <= 1:
            return [1.0]
        raw = [1.0 + decay_factor * (1.0 - 2.0 * i / (n_tranches - 1))
               for i in range(n_tranches)]
        # Normalize so sum == n_tranches (each weight is a multiplier of uniform size)
        total = sum(raw)
        return [w * n_tranches / total for w in raw]

    async def execute_alpha_decay(
        self,
        token_id: str,
        amount: float,
        target_price: float,
        decay_factor: float = 0.5,
        n_tranches: int = 4,
        interval: float = 2.0,
        paper: bool = True,
    ) -> ExecutionResult:
        """
        Public convenience method for alpha-decay execution.

        Creates an ExecutionPlan and delegates to execute_plan().
        """
        plan = ExecutionPlan(
            token_id=token_id,
            total_size=amount,
            target_price=target_price,
            strategy=ExecutionStrategy.ALPHA_DECAY,
            splits=n_tranches,
            interval_sec=interval,
            max_slippage=self.MAX_SLIPPAGE,
            decay_factor=decay_factor,
        )
        return await self.execute_plan(plan, paper=paper)

    async def _execute_alpha_decay(
        self, plan: ExecutionPlan, paper: bool
    ) -> ExecutionResult:
        """
        Front-loaded execution: bigger tranches first, smaller later.

        When alpha decays (e.g. weather forecast just released, breaking news),
        execute aggressively at the start when the information advantage is freshest.
        """
        n = plan.splits
        weights = self._alpha_decay_weights(n, plan.decay_factor)
        uniform_size = plan.total_size / n
        tranche_sizes = [w * uniform_size for w in weights]

        logger.info(
            f"[EXEC-ALPHA] Starting alpha-decay execution: "
            f"${plan.total_size:.2f} in {n} tranches, "
            f"decay_factor={plan.decay_factor:.2f}, "
            f"sizes=[{', '.join(f'${s:.2f}' for s in tranche_sizes)}]"
        )

        total_filled = 0.0
        fills = 0
        weighted_price_sum = 0.0

        for i in range(n):
            tranche_size = tranche_sizes[i]

            # Adjust last tranche for rounding remainders
            remaining = plan.total_size - total_filled
            if remaining < tranche_size:
                tranche_size = remaining
            if tranche_size < 1.0:
                logger.info(
                    f"[EXEC-ALPHA] Tranche {i+1}/{n} size ${tranche_size:.2f} < $1 — done"
                )
                break

            if paper:
                import random
                slippage = random.uniform(0, plan.max_slippage)
                fill_price = plan.target_price * (1 + slippage)
                total_filled += tranche_size
                weighted_price_sum += fill_price * tranche_size
                fills += 1
                logger.info(
                    f"[EXEC-ALPHA] Tranche {i+1}/{n} PAPER: "
                    f"${tranche_size:.2f} @ {fill_price:.4f} "
                    f"(weight={weights[i]:.2f})"
                )
            elif self.api:
                try:
                    # Check current spread before each tranche
                    book = await asyncio.to_thread(
                        self.api.get_order_book, plan.token_id
                    )
                    asks = book.get("asks", [])
                    if asks:
                        current_ask = float(asks[0]["price"])
                        slippage = (
                            (current_ask - plan.target_price) / plan.target_price
                            if plan.target_price > 0 else 0
                        )

                        if slippage > plan.max_slippage:
                            logger.warning(
                                f"[EXEC-ALPHA] Tranche {i+1}/{n} "
                                f"slippage={slippage:.4f} > max={plan.max_slippage} — STOP"
                            )
                            break

                    shares = tranche_size / plan.target_price if plan.target_price > 0 else 0
                    result = await asyncio.to_thread(
                        self.api.smart_buy,
                        plan.token_id,
                        shares,
                        target_price=plan.target_price,
                    )

                    if result:
                        fill_price = result.get("fill_price", plan.target_price)
                        total_filled += tranche_size
                        weighted_price_sum += fill_price * tranche_size
                        fills += 1
                        logger.info(
                            f"[EXEC-ALPHA] Tranche {i+1}/{n} FILLED: "
                            f"${tranche_size:.2f} @ {fill_price:.4f} "
                            f"(weight={weights[i]:.2f})"
                        )
                    else:
                        logger.warning(
                            f"[EXEC-ALPHA] Tranche {i+1}/{n} fill fallito — STOP"
                        )
                        break
                except Exception as e:
                    logger.warning(
                        f"[EXEC-ALPHA] Tranche {i+1}/{n} errore: {e} — STOP"
                    )
                    break

            # Wait between tranches (except after the last)
            if i < n - 1:
                await asyncio.sleep(plan.interval_sec)

        avg_price = weighted_price_sum / total_filled if total_filled > 0 else plan.target_price
        overall_slippage = (
            abs(avg_price - plan.target_price) / plan.target_price
            if plan.target_price > 0 else 0
        )

        logger.info(
            f"[EXEC-ALPHA] Done: {fills}/{n} tranches filled, "
            f"${total_filled:.2f}/${plan.total_size:.2f}, "
            f"avg={avg_price:.4f}, slippage={overall_slippage:.4f}"
        )

        return ExecutionResult(
            total_filled=total_filled,
            fills=fills,
            avg_price=avg_price,
            slippage=overall_slippage,
            partial=total_filled < plan.total_size * 0.95,
        )

    # ------------------------------------------------------------------
    # Automatic execution mode selection
    # ------------------------------------------------------------------

    def choose_execution_mode(self, amount: float, signal_type: str) -> str:
        """
        Choose optimal execution mode based on order size and signal type.

        - Small orders (<=$30): single limit order (LIMIT_MAKER)
        - Time-sensitive signals with decaying alpha: front-loaded (ALPHA_DECAY)
        - Large orders without urgency: uniform split (TWAP)
        """
        if amount <= 30:
            return "LIMIT_MAKER"  # small order, single limit

        if signal_type in ("weather_latency", "event_breaking", "btc_sniper"):
            return "ALPHA_DECAY"  # time-sensitive signal, front-load

        return "TWAP"  # default for large orders
