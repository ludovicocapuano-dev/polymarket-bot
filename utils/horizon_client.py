"""
Horizon SDK Integration (v13.1)
==================================
PRIMARY execution engine for the Polymarket bot.

Routing logic:
  - size < $30:  Horizon limit order (maker, zero fee)
  - $30 <= size <= $100:  Horizon TWAP (5 slices, 30s)
  - size > $100:  Horizon VWAP (10% participation, 60s)
  - Fallback: native smart_buy/smart_sell if Horizon fails

Also provides:
  - cancel_all_orders()
  - get_positions()
  - Cross-exchange arb scanning (Polymarket + Kalshi)
  - Kelly sizing via Horizon
  - Walk-forward backtesting
  - Position sync with risk manager
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

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
    use_twap_above: float = 30.0   # use TWAP for orders >$30
    use_vwap_above: float = 100.0  # use VWAP for orders >$100
    enable_arb_scan: bool = True
    enable_smart_route: bool = True
    twap_slices: int = 5
    twap_duration_sec: int = 30
    vwap_participation: float = 0.1
    vwap_duration_sec: int = 60


@dataclass
class ExecutionResult:
    """Result of an execution attempt."""
    success: bool = False
    engine: str = "unknown"       # "horizon" or "native"
    algo: str = ""                # "limit", "twap", "vwap", "smart_buy", "smart_sell"
    size: float = 0.0
    price: float = 0.0
    fill_price: float = 0.0
    raw_result: Optional[dict] = None
    error: str = ""


class HorizonClient:
    """
    Unified Horizon SDK wrapper — PRIMARY execution engine.

    Strategies call execute_trade() which routes to the optimal algo.
    Falls back to native PolymarketAPI if Horizon is unavailable or errors.
    """

    def __init__(self, config: Optional[HorizonConfig] = None):
        self.config = config or HorizonConfig(
            api_key=os.getenv("HORIZON_API_KEY", ""),
            private_key=os.getenv("PRIVATE_KEY", ""),
        )
        self._pm: Optional[object] = None
        self._kalshi: Optional[object] = None
        self._connected = False

        # Native fallback functions — set by bot.py after init
        self._native_smart_buy: Optional[Callable] = None
        self._native_smart_sell: Optional[Callable] = None

        # Execution stats
        self._stats = {
            "horizon_orders": 0,
            "horizon_limit": 0,
            "horizon_twap": 0,
            "horizon_vwap": 0,
            "native_fallbacks": 0,
            "errors": 0,
        }

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

    def set_native_fallback(self, smart_buy: Callable, smart_sell: Callable):
        """Wire native API functions as fallback."""
        self._native_smart_buy = smart_buy
        self._native_smart_sell = smart_sell
        logger.info("[HORIZON] Native fallback functions wired")

    @property
    def available(self) -> bool:
        return HZ_AVAILABLE and self._connected

    # ── PRIMARY EXECUTION METHOD ──────────────────────────────────

    def execute_trade(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        strategy: str = "",
        inventory_frac: float = 0.0,
        volume_24h: float = 0.0,
        vpin: float = 0.0,
        allow_dead_book: bool = False,
        aggressive: bool = False,
    ) -> ExecutionResult:
        """
        Execute a trade via the optimal algorithm.

        Routing:
          size < $30   → Horizon limit order (maker, zero fee)
          $30-$100     → Horizon TWAP (5 slices, 30s)
          > $100       → Horizon VWAP (10% participation, 60s)

        Falls back to native smart_buy/smart_sell if Horizon fails.

        Args:
            token_id: Polymarket token ID
            side: "BUY" or "SELL"
            size: dollar amount
            price: target price
            strategy: strategy name for logging
            inventory_frac: A-S inventory fraction (passed to native fallback)
            volume_24h: 24h volume (passed to native fallback)
            vpin: VPIN value (passed to native fallback)

        Returns:
            ExecutionResult with success flag, engine used, and details
        """
        is_buy = "BUY" in side.upper()
        tag = f"[HORIZON] [{strategy}]" if strategy else "[HORIZON]"

        # Try Horizon first
        if self.available:
            try:
                result = self._horizon_execute(token_id, side, size, price, tag)
                if result and result.success:
                    self._stats["horizon_orders"] += 1
                    return result
            except Exception as e:
                self._stats["errors"] += 1
                logger.warning(f"{tag} Horizon error, falling back to native: {e}")

        # Fallback to native API
        return self._native_execute(
            token_id, side, size, price, tag,
            inventory_frac=inventory_frac,
            volume_24h=volume_24h,
            vpin=vpin,
            allow_dead_book=allow_dead_book,
            aggressive=aggressive,
        )

    def _horizon_execute(
        self, token_id: str, side: str, size: float, price: float, tag: str
    ) -> Optional[ExecutionResult]:
        """Route to optimal Horizon algo based on size."""
        # v13.3: Horizon SDK 0.5.0 API is async (TWAP.start/VWAP.start) and
        # SmartRouter.route is a decisional router, not an executor.
        # Skip Horizon and go straight to native CLOB execution for now.
        # This avoids the error-then-fallback overhead every cycle.
        return None

    def _execute_limit(
        self, token_id: str, hz_side, hz_order_side, size: float, price: float, tag: str
    ) -> Optional[ExecutionResult]:
        """Limit order via Horizon (maker, zero fee). For size < $30."""
        try:
            order = hz.OrderRequest(
                market_id=token_id,
                side=hz_side,
                order_side=hz_order_side,
                size=size,
                price=price,
            )
            result = hz.SmartRouter.execute(order)
            self._stats["horizon_limit"] += 1
            fill_price = getattr(result, 'fill_price', price) if result else price
            logger.info(
                f"{tag} LIMIT {'BUY' if hz_side == hz.Side.Yes else 'SELL'} "
                f"${size:.0f} @ {price:.3f} → fill={fill_price:.3f}"
            )
            return ExecutionResult(
                success=True,
                engine="horizon",
                algo="limit",
                size=size,
                price=price,
                fill_price=fill_price,
                raw_result={"status": "filled", "algo": "limit", "size": size,
                            "_fill_price": fill_price},
            )
        except Exception as e:
            logger.warning(f"{tag} Limit order error: {e}")
            return None

    def _execute_twap(
        self, token_id: str, hz_side, hz_order_side, size: float, price: float, tag: str
    ) -> Optional[ExecutionResult]:
        """TWAP execution for medium orders ($30-$100)."""
        try:
            order = hz.OrderRequest(
                market_id=token_id,
                side=hz_side,
                order_side=hz_order_side,
                size=size,
                price=price,
            )
            result = hz.TWAP.execute(
                order,
                slices=self.config.twap_slices,
                duration_sec=self.config.twap_duration_sec,
            )
            self._stats["horizon_twap"] += 1
            fill_price = getattr(result, 'fill_price', price) if result else price
            logger.info(
                f"{tag} TWAP {'BUY' if hz_side == hz.Side.Yes else 'SELL'} "
                f"${size:.0f} @ {price:.3f} ({self.config.twap_slices} slices, "
                f"{self.config.twap_duration_sec}s) → fill={fill_price:.3f}"
            )
            return ExecutionResult(
                success=True,
                engine="horizon",
                algo="twap",
                size=size,
                price=price,
                fill_price=fill_price,
                raw_result={"status": "filled", "algo": "TWAP", "size": size,
                            "_fill_price": fill_price},
            )
        except Exception as e:
            logger.warning(f"{tag} TWAP error: {e}")
            return None

    def _execute_vwap(
        self, token_id: str, hz_side, hz_order_side, size: float, price: float, tag: str
    ) -> Optional[ExecutionResult]:
        """VWAP execution for large orders (>$100)."""
        try:
            order = hz.OrderRequest(
                market_id=token_id,
                side=hz_side,
                order_side=hz_order_side,
                size=size,
                price=price,
            )
            result = hz.VWAP.execute(
                order,
                participation_rate=self.config.vwap_participation,
                duration_sec=self.config.vwap_duration_sec,
            )
            self._stats["horizon_vwap"] += 1
            fill_price = getattr(result, 'fill_price', price) if result else price
            logger.info(
                f"{tag} VWAP {'BUY' if hz_side == hz.Side.Yes else 'SELL'} "
                f"${size:.0f} @ {price:.3f} ({self.config.vwap_participation*100:.0f}% "
                f"participation, {self.config.vwap_duration_sec}s) → fill={fill_price:.3f}"
            )
            return ExecutionResult(
                success=True,
                engine="horizon",
                algo="vwap",
                size=size,
                price=price,
                fill_price=fill_price,
                raw_result={"status": "filled", "algo": "VWAP", "size": size,
                            "_fill_price": fill_price},
            )
        except Exception as e:
            logger.warning(f"{tag} VWAP error: {e}")
            return None

    # ── NATIVE FALLBACK ──────────────────────────────────────────

    def _native_execute(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        tag: str,
        inventory_frac: float = 0.0,
        volume_24h: float = 0.0,
        vpin: float = 0.0,
        allow_dead_book: bool = False,
        aggressive: bool = False,
    ) -> ExecutionResult:
        """Fallback to native PolymarketAPI smart_buy/smart_sell."""
        is_buy = "BUY" in side.upper()

        if is_buy and self._native_smart_buy:
            try:
                shares = size / price if price > 0 else 0
                result = self._native_smart_buy(
                    token_id, shares,
                    target_price=price,
                    inventory_frac=inventory_frac,
                    volume_24h=volume_24h,
                    vpin=vpin,
                    allow_dead_book=allow_dead_book,
                    aggressive=aggressive,
                )
                self._stats["native_fallbacks"] += 1
                if result:
                    fill_price = price
                    if isinstance(result, dict) and result.get("_fill_price"):
                        fill_price = result["_fill_price"]
                    logger.info(f"{tag} [NATIVE] smart_buy ${size:.0f} @ {price:.3f}")
                    return ExecutionResult(
                        success=True,
                        engine="native",
                        algo="smart_buy",
                        size=size,
                        price=price,
                        fill_price=fill_price,
                        raw_result=result,
                    )
                else:
                    logger.warning(f"{tag} [NATIVE] smart_buy returned None")
                    return ExecutionResult(
                        success=False, engine="native", algo="smart_buy",
                        error="smart_buy returned None",
                    )
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"{tag} [NATIVE] smart_buy error: {e}")
                return ExecutionResult(
                    success=False, engine="native", algo="smart_buy",
                    error=str(e),
                )

        elif not is_buy and self._native_smart_sell:
            try:
                shares = size / price if price > 0 else 0
                result = self._native_smart_sell(
                    token_id, shares,
                    current_price=price,
                )
                self._stats["native_fallbacks"] += 1
                if result:
                    logger.info(f"{tag} [NATIVE] smart_sell ${size:.0f} @ {price:.3f}")
                    return ExecutionResult(
                        success=True,
                        engine="native",
                        algo="smart_sell",
                        size=size,
                        price=price,
                        fill_price=price,
                        raw_result=result,
                    )
                else:
                    return ExecutionResult(
                        success=False, engine="native", algo="smart_sell",
                        error="smart_sell returned None",
                    )
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"{tag} [NATIVE] smart_sell error: {e}")
                return ExecutionResult(
                    success=False, engine="native", algo="smart_sell",
                    error=str(e),
                )
        else:
            logger.error(f"{tag} No execution method available (side={side})")
            return ExecutionResult(
                success=False, engine="none", error="No execution method available",
            )

    # ── ORDER MANAGEMENT ─────────────────────────────────────────

    def cancel_all_orders(self) -> int:
        """Cancel all open orders via Horizon. Returns count of cancelled orders."""
        if not self.available:
            logger.warning("[HORIZON] Cannot cancel orders — not connected")
            return 0

        try:
            result = self._pm.cancel_all()
            count = getattr(result, 'count', 0) if result else 0
            logger.info(f"[HORIZON] Cancelled {count} open orders")
            return count
        except Exception as e:
            logger.warning(f"[HORIZON] cancel_all_orders error: {e}")
            return 0

    # ── POSITION QUERIES ─────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """Get all open positions via Horizon."""
        if not self.available:
            return []

        try:
            positions = self._pm.get_positions()
            if not positions:
                return []

            result = []
            for pos in positions:
                result.append({
                    "token_id": getattr(pos, 'token_id', ''),
                    "market_id": getattr(pos, 'market_id', ''),
                    "side": getattr(pos, 'side', ''),
                    "size": getattr(pos, 'size', 0),
                    "avg_price": getattr(pos, 'avg_price', 0),
                    "current_price": getattr(pos, 'current_price', 0),
                    "unrealized_pnl": getattr(pos, 'unrealized_pnl', 0),
                })
            logger.info(f"[HORIZON] Retrieved {len(result)} positions")
            return result
        except Exception as e:
            logger.debug(f"[HORIZON] get_positions error: {e}")
            return []

    def sync_positions_with_risk(self, risk_manager) -> dict:
        """
        Sync Horizon positions with risk manager.
        Detects positions that exist on-chain but aren't tracked.

        Returns:
            dict with sync stats: tracked, untracked, synced
        """
        hz_positions = self.get_positions()
        if not hz_positions:
            return {"tracked": 0, "untracked": 0, "synced": 0}

        tracked_tokens = {t.token_id for t in risk_manager.open_trades.values()
                          if hasattr(t, 'token_id')}

        untracked = []
        for pos in hz_positions:
            token_id = pos.get("token_id", "")
            if token_id and token_id not in tracked_tokens:
                untracked.append(pos)

        if untracked:
            logger.warning(
                f"[HORIZON-SYNC] Found {len(untracked)} untracked positions: "
                + ", ".join(p.get("token_id", "?")[:16] for p in untracked[:5])
            )

        stats = {
            "tracked": len(tracked_tokens),
            "untracked": len(untracked),
            "synced": len(hz_positions),
            "untracked_positions": untracked,
        }
        logger.info(
            f"[HORIZON-SYNC] Positions: {stats['synced']} on-chain, "
            f"{stats['tracked']} tracked, {stats['untracked']} untracked"
        )
        return stats

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

    # ── Status ────────────────────────────────────────────────

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
            "stats": dict(self._stats),
        }
