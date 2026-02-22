"""
Execution Engine v9.0 — Ottimizzazione piazzamento ordini.

Strategie di esecuzione:
- LIMIT_MAKER: standard smart_buy per trade <= $30
- TWAP: tranche da $15 ogni 2s per trade > $30
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
