"""
VPIN Monitor v9.2.1 — Volume-synchronized Probability of Informed Trading.

Basato su: Easley, López de Prado, O'Hara (2012) "Flow Toxicity and Liquidity
in a High-Frequency World"

VPIN misura la probabilità di informed trading (toxic flow) in un mercato.
VPIN alto → market maker a rischio → spread si allarga → prezzo instabile.

Per il bot: VPIN > soglia → blocca nuovi trade (il mercato è "tossico").

Implementazione:
- Bulk Volume Classification (BVC) via CDF normale per classificare
  ogni trade come buy/sell senza tick rule
- Volume buckets di dimensione fissa (VBS)
- VPIN = sum(|V_sell - V_buy|) / (n_buckets * VBS)
"""

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Parametri VPIN
DEFAULT_VBS = 50.0         # Volume Bucket Size in $ (Polymarket è low-volume)
DEFAULT_N_BUCKETS = 20     # Numero di bucket per il calcolo VPIN
VPIN_TOXIC_THRESHOLD = 0.7 # VPIN > 0.7 = mercato tossico (informed trading)
VPIN_WARN_THRESHOLD = 0.5  # VPIN > 0.5 = warning (da monitorare)
SIGMA_WINDOW = 50          # Ultimi N trade per stimare sigma (volatilità)


@dataclass
class VolumeBucket:
    """Un bucket di volume completato."""
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    completed_at: float = 0.0

    @property
    def imbalance(self) -> float:
        return abs(self.buy_volume - self.sell_volume)


class MarketVPIN:
    """Tracker VPIN per un singolo mercato."""

    def __init__(self, vbs: float = DEFAULT_VBS, n_buckets: int = DEFAULT_N_BUCKETS):
        self.vbs = vbs
        self.n_buckets = n_buckets

        # Bucket corrente (in filling)
        self._current_buy: float = 0.0
        self._current_sell: float = 0.0
        self._current_vol: float = 0.0

        # Bucket completati (rolling window)
        self._buckets: deque[VolumeBucket] = deque(maxlen=n_buckets)

        # Storico prezzi per stimare sigma
        self._prices: deque[float] = deque(maxlen=SIGMA_WINDOW)
        self._last_price: float = 0.0

        self._total_trades: int = 0
        self._last_update: float = 0.0

    def record_trade(self, price: float, size: float) -> None:
        """
        Registra un trade e classifica come buy/sell via BVC.

        BVC (Easley et al. 2012): usa la CDF normale per stimare la
        probabilità che un trade sia un buy, basandosi sulla variazione
        di prezzo normalizzata per la volatilità.
        """
        self._total_trades += 1
        self._last_update = time.time()

        # Calcola delta price normalizzato
        if self._last_price > 0:
            delta = price - self._last_price
        else:
            delta = 0.0

        self._prices.append(price)
        self._last_price = price

        # Stima sigma (volatilità) dai prezzi recenti
        sigma = self._estimate_sigma()

        # BVC: probabilità che il trade sia un buy
        if sigma > 0 and delta != 0:
            z = delta / sigma
            buy_prob = _normal_cdf(z)
        else:
            buy_prob = 0.5  # Se non c'è info, 50/50

        # Classifica il volume
        buy_vol = size * buy_prob
        sell_vol = size * (1.0 - buy_prob)

        self._current_buy += buy_vol
        self._current_sell += sell_vol
        self._current_vol += size

        # Se il bucket è pieno, chiudilo e inizia il prossimo
        while self._current_vol >= self.vbs:
            overflow = self._current_vol - self.vbs

            # Proporziona l'overflow
            if self._current_vol > 0:
                ratio = overflow / self._current_vol
            else:
                ratio = 0.0

            bucket_buy = self._current_buy * (1 - ratio)
            bucket_sell = self._current_sell * (1 - ratio)

            self._buckets.append(VolumeBucket(
                buy_volume=bucket_buy,
                sell_volume=bucket_sell,
                completed_at=time.time(),
            ))

            # Il residuo va nel prossimo bucket
            self._current_buy = self._current_buy * ratio
            self._current_sell = self._current_sell * ratio
            self._current_vol = overflow

    def _estimate_sigma(self) -> float:
        """Stima volatilità come deviazione standard dei log-returns."""
        if len(self._prices) < 3:
            return 0.0

        prices = list(self._prices)
        log_returns = []
        for i in range(1, len(prices)):
            if prices[i] > 0 and prices[i - 1] > 0:
                log_returns.append(math.log(prices[i] / prices[i - 1]))

        if len(log_returns) < 2:
            return 0.0

        mean = sum(log_returns) / len(log_returns)
        var = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        return math.sqrt(var) if var > 0 else 0.0

    @property
    def vpin(self) -> float:
        """
        Calcola VPIN corrente.

        VPIN = sum(|V_buy_i - V_sell_i|) / (n * VBS)

        Ritorna 0.0 se non ci sono abbastanza bucket.
        """
        if len(self._buckets) < 2:
            return 0.0

        total_imbalance = sum(b.imbalance for b in self._buckets)
        n = len(self._buckets)
        return total_imbalance / (n * self.vbs)

    @property
    def is_toxic(self) -> bool:
        return self.vpin >= VPIN_TOXIC_THRESHOLD

    @property
    def total_trades(self) -> int:
        return self._total_trades


class VPINMonitor:
    """
    Monitor VPIN per tutti i mercati tracciati.

    Uso:
        monitor = VPINMonitor()
        monitor.record_trade(market_id, price, size)
        is_toxic, reason = monitor.check_toxicity(market_id)
    """

    def __init__(self, vbs: float = DEFAULT_VBS, n_buckets: int = DEFAULT_N_BUCKETS):
        self.vbs = vbs
        self.n_buckets = n_buckets
        self._markets: dict[str, MarketVPIN] = {}

    def record_trade(self, market_id: str, price: float, size: float) -> None:
        """Registra un trade per il calcolo VPIN."""
        if market_id not in self._markets:
            self._markets[market_id] = MarketVPIN(
                vbs=self.vbs, n_buckets=self.n_buckets,
            )
        self._markets[market_id].record_trade(price, size)

    def get_vpin(self, market_id: str) -> float:
        """Ritorna VPIN corrente per un mercato (0.0 se non tracciato)."""
        m = self._markets.get(market_id)
        if not m:
            return 0.0
        return m.vpin

    def check_toxicity(self, market_id: str) -> tuple[bool, str]:
        """
        Controlla se un mercato ha toxic flow (VPIN alto).

        Ritorna (True, reason) se tossico, (False, "") altrimenti.
        """
        m = self._markets.get(market_id)
        if not m:
            return False, ""

        vpin = m.vpin
        if vpin >= VPIN_TOXIC_THRESHOLD:
            return True, (
                f"VPIN toxic: {vpin:.3f} >= {VPIN_TOXIC_THRESHOLD} "
                f"su mercato {market_id[:12]} "
                f"({m.total_trades} trades, {len(m._buckets)} buckets)"
            )
        return False, ""

    def stats(self) -> dict:
        """Statistiche globali VPIN."""
        toxic = 0
        warn = 0
        tracked = len(self._markets)
        for m in self._markets.values():
            v = m.vpin
            if v >= VPIN_TOXIC_THRESHOLD:
                toxic += 1
            elif v >= VPIN_WARN_THRESHOLD:
                warn += 1
        return {
            "markets_tracked": tracked,
            "toxic_markets": toxic,
            "warning_markets": warn,
            "total_trades": sum(m.total_trades for m in self._markets.values()),
        }


def _normal_cdf(x: float) -> float:
    """
    Approssimazione CDF normale standard (Abramowitz & Stegun).

    Accuratezza ~1e-5, sufficiente per BVC.
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
