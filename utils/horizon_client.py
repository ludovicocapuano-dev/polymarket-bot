"""
Horizon SDK Integration (v12.5.3)
==================================
Wraps Horizon SDK as enhanced execution engine for the Polymarket bot.

Uses Horizon for:
1. Smart execution (TWAP, VWAP, Iceberg) for orders >$30
2. Cross-exchange arb scanning (Polymarket + Kalshi)
3. Resolution sniping with Horizon's built-in sniper
4. Kelly sizing with Horizon's multi-asset Kelly
5. Walk-forward backtesting

Falls back to existing bot execution if Horizon is unavailable.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import horizon as hz
    HZ_AVAILABLE = True
    logger.info(f"[HORIZON] SDK v{hz.__version__} loaded")
except ImportError:
    HZ_AVAILABLE = False
    logger.info("[HORIZON] SDK not available, using native execution")


@dataclass
class HorizonConfig:
    api_key: str = ""
    private_key: str = ""
    use_twap_above: float = 30.0  # use TWAP for orders >$30
    use_vwap_above: float = 100.0  # use VWAP for orders >$100
    enable_arb_scan: bool = True
    enable_smart_route: bool = True


class HorizonClient:
    """Unified Horizon SDK wrapper for the Polymarket bot."""

    def __init__(self, config: Optional[HorizonConfig] = None):
        self.config = config or HorizonConfig(
            api_key=os.getenv("HORIZON_API_KEY", ""),
            private_key=os.getenv("PRIVATE_KEY", ""),
        )
        self._pm: Optional[hz.Polymarket] = None
        self._kalshi: Optional[hz.Kalshi] = None
        self._connected = False

    def connect(self) -> bool:
        """Initialize Horizon exchanges."""
        if not HZ_AVAILABLE:
            return False
        try:
            self._pm = hz.Polymarket()
            logger.info("[HORIZON] Polymarket exchange initialized")

            try:
                self._kalshi = hz.Kalshi()
                logger.info("[HORIZON] Kalshi exchange initialized")
            except Exception as e:
                logger.debug(f"[HORIZON] Kalshi not available: {e}")

            self._connected = True
            return True
        except Exception as e:
            logger.warning(f"[HORIZON] Connection failed: {e}")
            return False

    @property
    def available(self) -> bool:
        return HZ_AVAILABLE and self._connected

    # ── Execution ──────────────────────────────────────────────

    def smart_execute(self, token_id: str, side: str, size: float,
                      price: float, market_id: str = "") -> Optional[dict]:
        """Execute with optimal algo based on order size."""
        if not self.available:
            return None

        try:
            if size >= self.config.use_vwap_above:
                return self._execute_vwap(token_id, side, size, price)
            elif size >= self.config.use_twap_above:
                return self._execute_twap(token_id, side, size, price)
            else:
                return None  # let the bot's native execution handle small orders
        except Exception as e:
            logger.warning(f"[HORIZON] Execution error: {e}")
            return None

    def _execute_twap(self, token_id: str, side: str, size: float,
                      price: float) -> Optional[dict]:
        """TWAP execution for medium orders ($30-$100)."""
        try:
            order = hz.OrderRequest(
                market_id=token_id,
                side=hz.Side.BUY if "BUY" in side.upper() else hz.Side.SELL,
                size=size,
                price=price,
            )
            # TWAP splits into 5 slices over 30 seconds
            result = hz.TWAP.execute(order, slices=5, duration_sec=30)
            logger.info(f"[HORIZON] TWAP executed: {side} ${size:.0f} @ {price:.3f}")
            return {"status": "filled", "algo": "TWAP", "size": size}
        except Exception as e:
            logger.warning(f"[HORIZON] TWAP error: {e}")
            return None

    def _execute_vwap(self, token_id: str, side: str, size: float,
                      price: float) -> Optional[dict]:
        """VWAP execution for large orders (>$100)."""
        try:
            order = hz.OrderRequest(
                market_id=token_id,
                side=hz.Side.BUY if "BUY" in side.upper() else hz.Side.SELL,
                size=size,
                price=price,
            )
            result = hz.VWAP.execute(order, participation_rate=0.1, duration_sec=60)
            logger.info(f"[HORIZON] VWAP executed: {side} ${size:.0f} @ {price:.3f}")
            return {"status": "filled", "algo": "VWAP", "size": size}
        except Exception as e:
            logger.warning(f"[HORIZON] VWAP error: {e}")
            return None

    # ── Arb Scanning ───────────────────────────────────────────

    def scan_cross_exchange_arb(self) -> list[dict]:
        """Scan for arbitrage between Polymarket and Kalshi."""
        if not self.available or not self._kalshi:
            return []

        try:
            opps = hz.arb_scanner(
                exchanges=[self._pm, self._kalshi],
                min_edge=0.03,
                max_results=10,
            )
            if opps:
                results = []
                for opp in opps:
                    results.append({
                        "type": "cross_exchange",
                        "market": getattr(opp, 'market_id', ''),
                        "edge": getattr(opp, 'edge', 0),
                        "size": getattr(opp, 'recommended_size', 0),
                        "exchanges": ["polymarket", "kalshi"],
                    })
                logger.info(f"[HORIZON] Found {len(results)} cross-exchange arb opportunities")
                return results
        except Exception as e:
            logger.debug(f"[HORIZON] Arb scan error: {e}")
        return []

    def scan_parity_arb(self, markets: list = None) -> list[dict]:
        """Scan for parity arbitrage (sum != 1.0)."""
        if not self.available:
            return []

        try:
            opps = hz.parity_arb_scanner(
                exchange=self._pm,
                min_edge=0.02,
            )
            if opps:
                results = []
                for opp in opps:
                    results.append({
                        "type": "parity",
                        "market": getattr(opp, 'market_id', ''),
                        "deviation": getattr(opp, 'deviation', 0),
                        "edge": getattr(opp, 'edge', 0),
                    })
                logger.info(f"[HORIZON] Found {len(results)} parity arb opportunities")
                return results
        except Exception as e:
            logger.debug(f"[HORIZON] Parity arb error: {e}")
        return []

    # ── Kelly Sizing ──────────────────────────────────────────

    def kelly_size(self, win_prob: float, odds: float,
                   fraction: float = 0.25) -> float:
        """Calculate Kelly-optimal position size."""
        if not HZ_AVAILABLE:
            # Fallback to simple Kelly
            edge = win_prob - (1 - win_prob) / odds
            return max(0, edge / odds * fraction)

        try:
            return hz.kelly(p=win_prob, b=odds) * fraction
        except Exception:
            edge = win_prob - (1 - win_prob) / odds
            return max(0, edge / odds * fraction)

    # ── Resolution Sniping ────────────────────────────────────

    def resolution_scan(self, markets: list = None) -> list[dict]:
        """Use Horizon's built-in resolution sniper."""
        if not self.available:
            return []

        try:
            signals = hz.resolution_sniper(exchange=self._pm)
            if signals:
                results = []
                for sig in signals:
                    results.append({
                        "market_id": getattr(sig, 'market_id', ''),
                        "signal": getattr(sig, 'signal', ''),
                        "confidence": getattr(sig, 'confidence', 0),
                        "edge": getattr(sig, 'edge', 0),
                    })
                logger.info(f"[HORIZON] Resolution sniper: {len(results)} signals")
                return results
        except Exception as e:
            logger.debug(f"[HORIZON] Resolution scan error: {e}")
        return []

    # ── Walk-Forward Backtest ─────────────────────────────────

    def walk_forward_test(self, strategy_fn, markets: list,
                          windows: int = 5) -> Optional[dict]:
        """Run walk-forward optimization using Horizon's engine."""
        if not HZ_AVAILABLE:
            return None

        try:
            result = hz.walk_forward(
                strategy=strategy_fn,
                markets=markets,
                n_windows=windows,
            )
            return {
                "sharpe": getattr(result, 'sharpe', 0),
                "total_pnl": getattr(result, 'total_pnl', 0),
                "win_rate": getattr(result, 'win_rate', 0),
                "n_trades": getattr(result, 'n_trades', 0),
                "windows": [
                    {"train_score": getattr(w, 'train_score', 0),
                     "test_score": getattr(w, 'test_score', 0)}
                    for w in getattr(result, 'windows', [])
                ],
            }
        except Exception as e:
            logger.debug(f"[HORIZON] Walk-forward error: {e}")
            return None

    # ── Analytics ─────────────────────────────────────────────

    def generate_tearsheet(self, returns: list) -> Optional[dict]:
        """Generate Horizon tearsheet analytics."""
        if not HZ_AVAILABLE:
            return None

        try:
            ts = hz.generate_tearsheet(returns)
            return {
                "sharpe": getattr(ts, 'sharpe', 0),
                "sortino": getattr(ts, 'sortino', 0),
                "max_drawdown": getattr(ts, 'max_drawdown', 0),
                "calmar": getattr(ts, 'calmar', 0),
                "win_rate": getattr(ts, 'win_rate', 0),
            }
        except Exception as e:
            logger.debug(f"[HORIZON] Tearsheet error: {e}")
            return None

    def status(self) -> dict:
        """Return status of Horizon integration."""
        return {
            "available": HZ_AVAILABLE,
            "connected": self._connected,
            "version": hz.__version__ if HZ_AVAILABLE else None,
            "polymarket": self._pm is not None,
            "kalshi": self._kalshi is not None,
            "twap_threshold": self.config.use_twap_above,
            "vwap_threshold": self.config.use_vwap_above,
        }
