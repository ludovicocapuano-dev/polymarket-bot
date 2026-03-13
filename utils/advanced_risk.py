"""
Advanced Risk Analytics v12.4 — arch, riskfolio-lib, pyfolio integration.

Provides:
1. GARCH model selection (GARCH/EGARCH/GJR-GARCH) via arch library
2. CVaR/MVO/HRP portfolio allocation via riskfolio-lib
3. PyFolio tearsheet analytics

All functions fall back to existing implementations on import/runtime errors.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# 1. ARCH Integration — proper GARCH model selection
# ─────────────────────────────────────────────────────────────────────

def fit_garch(returns: np.ndarray, horizon: int = 1) -> dict:
    """
    Fit GARCH(1,1), EGARCH(1,1), and GJR-GARCH(1,1) models.
    Pick the best by AIC. Return vol forecast, params, and AIC.

    Parameters
    ----------
    returns : np.ndarray
        Array of PnL returns (raw dollar PnL or percentage returns).
    horizon : int
        Forecast horizon in periods (default 1).

    Returns
    -------
    dict with keys:
        vol_forecast : float  — annualized vol forecast for next period
        params : dict         — fitted model parameters
        aic : float           — AIC of best model
        model_name : str      — name of selected model
        all_models : dict     — {name: aic} for all fitted models
        fallback : bool       — True if fell back to manual GARCH
    """
    if len(returns) < 10:
        return _garch_fallback(returns, reason="insufficient data (<10 obs)")

    try:
        from arch import arch_model
    except ImportError:
        return _garch_fallback(returns, reason="arch library not available")

    # Scale returns to percentage if they look like dollar amounts
    # arch works better with rescaled data
    ret_series = pd.Series(returns).dropna()
    if len(ret_series) < 10:
        return _garch_fallback(returns, reason="insufficient non-null data")

    # Rescale: arch expects ~percentage-scale data
    scale_factor = ret_series.std()
    if scale_factor < 1e-10:
        return _garch_fallback(returns, reason="near-zero variance")
    rescaled = ret_series / scale_factor * 100.0

    models_to_try = {
        "GARCH(1,1)": {"vol": "GARCH", "p": 1, "q": 1, "o": 0, "dist": "normal"},
        "EGARCH(1,1)": {"vol": "EGARCH", "p": 1, "q": 1, "o": 1, "dist": "normal"},
        "GJR-GARCH(1,1)": {"vol": "GARCH", "p": 1, "q": 1, "o": 1, "dist": "normal"},
    }

    results = {}
    best_aic = float("inf")
    best_name = None
    best_result = None

    for name, spec in models_to_try.items():
        try:
            am = arch_model(
                rescaled,
                mean="Constant",
                vol=spec["vol"],
                p=spec["p"],
                q=spec["q"],
                o=spec["o"],
                dist=spec["dist"],
            )
            res = am.fit(disp="off", show_warning=False)
            results[name] = res.aic
            if res.aic < best_aic:
                best_aic = res.aic
                best_name = name
                best_result = res
        except Exception as e:
            logger.debug(f"[GARCH] {name} fit failed: {e}")
            results[name] = float("inf")

    if best_result is None:
        return _garch_fallback(returns, reason="all arch models failed to fit")

    # Forecast
    try:
        fcast = best_result.forecast(horizon=horizon)
        # Variance forecast (rescaled back to original units)
        var_forecast = fcast.variance.iloc[-1].values[-1]
        # Undo rescaling: var_rescaled = var_original / scale^2 * 100^2
        vol_forecast = float(np.sqrt(var_forecast) * scale_factor / 100.0)
    except Exception as e:
        logger.debug(f"[GARCH] Forecast failed: {e}")
        vol_forecast = float(ret_series.std())

    # Extract params as plain dict
    params = {}
    try:
        for k, v in best_result.params.items():
            params[k] = float(v)
    except Exception:
        pass

    return {
        "vol_forecast": vol_forecast,
        "params": params,
        "aic": float(best_aic),
        "model_name": best_name,
        "all_models": {k: float(v) for k, v in results.items()},
        "fallback": False,
    }


def _garch_fallback(returns: np.ndarray, reason: str = "") -> dict:
    """
    Manual GARCH(1,1) fallback — mirrors risk_manager._recent_volatility().
    Used when arch library is not available or fitting fails.
    """
    logger.debug(f"[GARCH] Fallback to manual GARCH: {reason}")

    if len(returns) < 3:
        return {
            "vol_forecast": 0.0,
            "params": {},
            "aic": float("inf"),
            "model_name": "manual_GARCH(1,1)_fallback",
            "all_models": {},
            "fallback": True,
            "fallback_reason": reason,
        }

    EW_LAMBDA = 0.94
    data = list(returns)
    weights = [EW_LAMBDA ** i for i in range(len(data) - 1, -1, -1)]
    w_sum = sum(weights)
    mean_pnl = sum(w * x for w, x in zip(weights, data)) / w_sum
    residuals = [x - mean_pnl for x in data]
    var_unconditional = sum(w * r ** 2 for w, r in zip(weights, residuals)) / w_sum

    ALPHA, BETA = 0.06, 0.93
    OMEGA = var_unconditional * (1.0 - ALPHA - BETA)
    sigma_sq = var_unconditional
    for r in residuals:
        sigma_sq = OMEGA + ALPHA * r ** 2 + BETA * sigma_sq

    return {
        "vol_forecast": float(max(sigma_sq ** 0.5, 0.0)),
        "params": {"omega": OMEGA, "alpha": ALPHA, "beta": BETA},
        "aic": float("inf"),
        "model_name": "manual_GARCH(1,1)_fallback",
        "all_models": {},
        "fallback": True,
        "fallback_reason": reason,
    }


# ─────────────────────────────────────────────────────────────────────
# 2. Riskfolio-lib — CVaR / MVO / HRP allocation optimization
# ─────────────────────────────────────────────────────────────────────

def optimize_allocation(
    returns_df: pd.DataFrame,
    method: str = "CVaR",
    risk_free_rate: float = 0.0,
) -> dict:
    """
    Optimize portfolio allocation across strategies using riskfolio-lib.

    Parameters
    ----------
    returns_df : pd.DataFrame
        Columns = strategy names, rows = period returns.
        Minimum 20 rows recommended.
    method : str
        'MVO' — Mean-Variance Optimization (Markowitz)
        'CVaR' — Conditional Value at Risk optimization
        'HRP' — Hierarchical Risk Parity (Lopez de Prado)
    risk_free_rate : float
        Risk-free rate for Sharpe/optimization (default 0).

    Returns
    -------
    dict with keys:
        weights : dict[str, float]  — strategy -> optimal weight (sum=1.0)
        method : str                — method used
        fallback : bool             — True if fell back to equal-weight
        metrics : dict              — portfolio-level risk metrics
    """
    if returns_df.empty or len(returns_df) < 5:
        return _allocation_fallback(
            returns_df.columns.tolist(),
            reason="insufficient data (<5 periods)",
        )

    # Drop strategies with all-zero or all-NaN returns
    valid_cols = [
        c for c in returns_df.columns
        if returns_df[c].notna().sum() >= 5 and returns_df[c].std() > 1e-10
    ]
    if len(valid_cols) < 2:
        return _allocation_fallback(
            returns_df.columns.tolist(),
            reason="fewer than 2 strategies with valid data",
        )

    df = returns_df[valid_cols].dropna()
    if len(df) < 5:
        return _allocation_fallback(
            returns_df.columns.tolist(),
            reason="insufficient overlapping data after dropna",
        )

    try:
        import riskfolio as rp
    except ImportError:
        return _allocation_fallback(
            returns_df.columns.tolist(),
            reason="riskfolio-lib not available",
        )

    try:
        if method == "HRP":
            weights = _optimize_hrp(df, rp)
        elif method == "CVaR":
            weights = _optimize_cvar(df, rp, risk_free_rate)
        elif method == "MVO":
            weights = _optimize_mvo(df, rp, risk_free_rate)
        else:
            return _allocation_fallback(
                valid_cols,
                reason=f"unknown method: {method}",
            )
    except Exception as e:
        logger.warning(f"[ALLOC] {method} optimization failed: {e}")
        return _allocation_fallback(valid_cols, reason=str(e))

    # Compute portfolio metrics
    metrics = _compute_portfolio_metrics(df, weights)

    # Map back to all original columns (0 weight for excluded)
    full_weights = {c: 0.0 for c in returns_df.columns}
    for col, w in weights.items():
        full_weights[col] = round(float(w), 6)

    return {
        "weights": full_weights,
        "method": method,
        "fallback": False,
        "metrics": metrics,
    }


def _optimize_hrp(df: pd.DataFrame, rp) -> dict:
    """Hierarchical Risk Parity (Lopez de Prado)."""
    port = rp.HCPortfolio(returns=df)
    w = port.optimization(
        model="HRP",
        codependence="pearson",
        rm="MV",
        leaf_order=True,
    )
    return {col: float(w.loc[col, "weights"]) for col in df.columns}


def _optimize_cvar(df: pd.DataFrame, rp, rf: float) -> dict:
    """CVaR optimization — minimize CVaR subject to return target."""
    port = rp.Portfolio(returns=df)
    port.assets_stats(method_mu="hist", method_cov="hist")
    w = port.optimization(
        model="Classic",
        rm="CVaR",
        obj="MinRisk",
        rf=rf,
        hist=True,
    )
    if w is None:
        raise ValueError("CVaR optimization returned None (infeasible)")
    return {col: float(w.loc[col, "weights"]) for col in df.columns}


def _optimize_mvo(df: pd.DataFrame, rp, rf: float) -> dict:
    """Mean-Variance Optimization (Markowitz)."""
    port = rp.Portfolio(returns=df)
    port.assets_stats(method_mu="hist", method_cov="hist")
    w = port.optimization(
        model="Classic",
        rm="MV",
        obj="Sharpe",
        rf=rf,
        hist=True,
    )
    if w is None:
        raise ValueError("MVO optimization returned None (infeasible)")
    return {col: float(w.loc[col, "weights"]) for col in df.columns}


def _compute_portfolio_metrics(df: pd.DataFrame, weights: dict) -> dict:
    """Compute portfolio-level metrics from strategy returns and weights."""
    w_array = np.array([weights.get(c, 0.0) for c in df.columns])
    port_returns = df.values @ w_array

    if len(port_returns) < 2 or np.std(port_returns) < 1e-10:
        return {"sharpe": 0.0, "annual_return": 0.0, "annual_vol": 0.0}

    mean_r = float(np.mean(port_returns))
    std_r = float(np.std(port_returns, ddof=1))
    sharpe = mean_r / std_r if std_r > 0 else 0.0

    # CVaR 95% of portfolio
    sorted_r = np.sort(port_returns)
    cutoff = int(len(sorted_r) * 0.05)
    cvar_95 = float(-np.mean(sorted_r[:max(cutoff, 1)]))

    return {
        "sharpe": round(sharpe, 4),
        "mean_return": round(mean_r, 6),
        "vol": round(std_r, 6),
        "cvar_95": round(cvar_95, 6),
        "n_periods": len(port_returns),
    }


def _allocation_fallback(columns: list, reason: str = "") -> dict:
    """Equal-weight fallback when optimization is not possible."""
    logger.debug(f"[ALLOC] Fallback to equal-weight: {reason}")
    n = len(columns) if columns else 1
    w = 1.0 / n
    return {
        "weights": {c: round(w, 6) for c in columns},
        "method": "equal_weight_fallback",
        "fallback": True,
        "fallback_reason": reason,
        "metrics": {},
    }


# ─────────────────────────────────────────────────────────────────────
# 3. PyFolio Analytics — tearsheet metrics + HTML report
# ─────────────────────────────────────────────────────────────────────

def generate_tearsheet(
    returns: pd.Series,
    benchmark: pd.Series | None = None,
    output_path: str = "logs/pyfolio_report.html",
) -> dict:
    """
    Generate performance analytics using pyfolio / empyrical.

    Parameters
    ----------
    returns : pd.Series
        Daily returns series with DatetimeIndex.
    benchmark : pd.Series, optional
        Benchmark returns for comparison.
    output_path : str
        Path for HTML tearsheet report.

    Returns
    -------
    dict with keys:
        sharpe, sortino, max_drawdown, calmar, annual_return, annual_vol,
        tail_ratio, stability, report_path, fallback
    """
    if returns.empty or len(returns) < 3:
        return _tearsheet_fallback(reason="insufficient data")

    # Clean returns
    returns = returns.dropna()
    if len(returns) < 3:
        return _tearsheet_fallback(reason="insufficient non-null data")

    # Try empyrical first (lighter, always works if installed)
    metrics = _compute_empyrical_metrics(returns, benchmark)

    # Try generating HTML tearsheet via pyfolio
    report_path = _generate_pyfolio_html(returns, benchmark, output_path)
    if report_path:
        metrics["report_path"] = report_path

    return metrics


def _compute_empyrical_metrics(
    returns: pd.Series,
    benchmark: pd.Series | None = None,
) -> dict:
    """Compute core metrics using empyrical-reloaded."""
    try:
        import empyrical
    except ImportError:
        return _tearsheet_fallback(reason="empyrical not available")

    try:
        result = {
            "sharpe": _safe_float(empyrical.sharpe_ratio(returns)),
            "sortino": _safe_float(empyrical.sortino_ratio(returns)),
            "max_drawdown": _safe_float(empyrical.max_drawdown(returns)),
            "calmar": _safe_float(empyrical.calmar_ratio(returns)),
            "annual_return": _safe_float(empyrical.annual_return(returns)),
            "annual_vol": _safe_float(empyrical.annual_volatility(returns)),
            "tail_ratio": _safe_float(empyrical.tail_ratio(returns)),
            "stability": _safe_float(empyrical.stability_of_timeseries(returns)),
            "fallback": False,
        }

        # Alpha/Beta vs benchmark
        if benchmark is not None and len(benchmark) >= 3:
            try:
                alpha, beta = empyrical.alpha_beta(returns, benchmark)
                result["alpha"] = _safe_float(alpha)
                result["beta"] = _safe_float(beta)
            except Exception:
                pass

        return result

    except Exception as e:
        logger.warning(f"[PYFOLIO] empyrical metrics failed: {e}")
        return _tearsheet_fallback(reason=str(e))


def _generate_pyfolio_html(
    returns: pd.Series,
    benchmark: pd.Series | None,
    output_path: str,
) -> str | None:
    """Generate pyfolio HTML tearsheet. Returns path on success, None on failure."""
    try:
        import pyfolio as pf
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
    except ImportError:
        logger.debug("[PYFOLIO] pyfolio or matplotlib not available for HTML report")
        return None

    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        fig = plt.figure(figsize=(16, 20))

        # Use pyfolio's plotting functions individually for more control
        try:
            if benchmark is not None and len(benchmark) >= 3:
                pf.plotting.show_perf_stats(
                    returns, benchmark, positions=None, transactions=None
                )
            # Create returns tearsheet
            pf.plotting.plot_returns(returns, ax=fig.add_subplot(4, 1, 1))
            pf.plotting.plot_drawdown_underwater(returns, ax=fig.add_subplot(4, 1, 2))
            pf.plotting.plot_monthly_returns_heatmap(returns, ax=fig.add_subplot(4, 1, 3))
            pf.plotting.plot_return_quantiles(returns, ax=fig.add_subplot(4, 1, 4))
        except Exception as e:
            # Some pyfolio versions have different APIs
            logger.debug(f"[PYFOLIO] Partial plot failure (non-critical): {e}")

        fig.tight_layout()
        fig.savefig(output_path, dpi=100, bbox_inches="tight")
        plt.close(fig)

        logger.info(f"[PYFOLIO] Report saved to {output_path}")
        return output_path

    except Exception as e:
        logger.warning(f"[PYFOLIO] HTML report generation failed: {e}")
        plt.close("all")
        return None


def _tearsheet_fallback(reason: str = "") -> dict:
    """Fallback when pyfolio/empyrical are not available."""
    logger.debug(f"[PYFOLIO] Fallback: {reason}")
    return {
        "sharpe": 0.0,
        "sortino": 0.0,
        "max_drawdown": 0.0,
        "calmar": 0.0,
        "annual_return": 0.0,
        "annual_vol": 0.0,
        "tail_ratio": 0.0,
        "stability": 0.0,
        "fallback": True,
        "fallback_reason": reason,
    }


def _safe_float(val) -> float:
    """Convert to float, handling NaN/Inf."""
    try:
        f = float(val)
        if np.isnan(f) or np.isinf(f):
            return 0.0
        return round(f, 6)
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────────────────────────────
# 4. Unified analysis runner — called from bot.py every 1000 cycles
# ─────────────────────────────────────────────────────────────────────

def run_advanced_risk_analysis(risk_manager) -> dict:
    """
    Run all advanced risk analytics and return a summary report.
    Called from bot.py every 1000 cycles.

    Does NOT auto-apply changes — only logs recommendations.

    Parameters
    ----------
    risk_manager : RiskManager
        The bot's active risk manager instance.

    Returns
    -------
    dict with sections: garch, allocation, tearsheet
    """
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "garch": {},
        "allocation": {},
        "tearsheet": {},
    }

    # ── 1. GARCH analysis per strategy ──
    try:
        report["garch"] = _analyze_garch_per_strategy(risk_manager)
    except Exception as e:
        logger.warning(f"[ADVANCED_RISK] GARCH analysis failed: {e}")
        report["garch"] = {"error": str(e)}

    # ── 2. Portfolio allocation optimization ──
    try:
        report["allocation"] = _analyze_allocation(risk_manager)
    except Exception as e:
        logger.warning(f"[ADVANCED_RISK] Allocation analysis failed: {e}")
        report["allocation"] = {"error": str(e)}

    # ── 3. PyFolio tearsheet ──
    try:
        report["tearsheet"] = _analyze_tearsheet(risk_manager)
    except Exception as e:
        logger.warning(f"[ADVANCED_RISK] Tearsheet analysis failed: {e}")
        report["tearsheet"] = {"error": str(e)}

    # Save report
    try:
        os.makedirs("logs", exist_ok=True)
        report_path = "logs/advanced_risk_report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"[ADVANCED_RISK] Report saved to {report_path}")
    except Exception as e:
        logger.debug(f"[ADVANCED_RISK] Could not save report: {e}")

    return report


def _analyze_garch_per_strategy(risk_manager) -> dict:
    """Run GARCH model selection for each active strategy."""
    results = {}
    strategies_seen = set()

    for t in risk_manager.trades:
        if t.result in ("WIN", "LOSS"):
            strategies_seen.add(t.strategy)

    for strategy in strategies_seen:
        returns = np.array([
            t.pnl for t in risk_manager.trades
            if t.strategy == strategy and t.result in ("WIN", "LOSS")
        ])
        if len(returns) < 5:
            continue

        garch_result = fit_garch(returns)
        results[strategy] = garch_result

        # Compare with manual GARCH
        manual_vol = risk_manager._recent_volatility(strategy)
        results[strategy]["manual_vol"] = float(manual_vol)
        diff_pct = 0.0
        if manual_vol > 0:
            diff_pct = (garch_result["vol_forecast"] - manual_vol) / manual_vol * 100
        results[strategy]["vol_diff_pct"] = round(diff_pct, 1)

        logger.info(
            f"[ADVANCED_RISK] GARCH {strategy}: "
            f"model={garch_result['model_name']} "
            f"vol={garch_result['vol_forecast']:.4f} "
            f"(manual={manual_vol:.4f}, diff={diff_pct:+.1f}%) "
            f"AIC={garch_result['aic']:.1f}"
        )

    return results


def _analyze_allocation(risk_manager) -> dict:
    """Build strategy returns matrix and optimize allocation."""
    # Build returns DataFrame from closed trades
    strategy_pnls = {}
    for t in risk_manager.trades:
        if t.result in ("WIN", "LOSS"):
            strategy_pnls.setdefault(t.strategy, []).append(t.pnl)

    if len(strategy_pnls) < 2:
        return {"skipped": True, "reason": "fewer than 2 strategies with closed trades"}

    # Pad to equal length (align by trade index)
    max_len = max(len(v) for v in strategy_pnls.values())
    if max_len < 10:
        return {"skipped": True, "reason": f"insufficient trades ({max_len})"}

    # Create DataFrame — pad shorter series with 0
    data = {}
    for strat, pnls in strategy_pnls.items():
        padded = pnls + [0.0] * (max_len - len(pnls))
        data[strat] = padded

    returns_df = pd.DataFrame(data)

    # Run all three methods
    results = {}
    for method in ["CVaR", "MVO", "HRP"]:
        alloc = optimize_allocation(returns_df, method=method)
        results[method] = alloc

        if not alloc.get("fallback"):
            # Log the recommended weights
            weights_str = ", ".join(
                f"{k}={v:.1%}" for k, v in sorted(
                    alloc["weights"].items(), key=lambda x: -x[1]
                ) if v > 0.01
            )
            logger.info(
                f"[ADVANCED_RISK] {method} allocation: {weights_str} "
                f"(sharpe={alloc.get('metrics', {}).get('sharpe', 0):.3f})"
            )

    # Compare with current allocation
    current = {}
    for strat, budget in risk_manager._strategy_budgets.items():
        if budget > 0:
            total = sum(risk_manager._strategy_budgets.values())
            current[strat] = budget / total if total > 0 else 0
    results["current_allocation"] = current

    return results


def _analyze_tearsheet(risk_manager) -> dict:
    """Generate pyfolio tearsheet from all closed trades."""
    closed = [t for t in risk_manager.trades if t.result in ("WIN", "LOSS")]
    if len(closed) < 5:
        return {"skipped": True, "reason": f"insufficient closed trades ({len(closed)})"}

    # Build daily returns series
    daily_pnl = {}
    for t in closed:
        day = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
        daily_pnl[day] = daily_pnl.get(day, 0.0) + t.pnl

    if len(daily_pnl) < 3:
        return {"skipped": True, "reason": "fewer than 3 trading days"}

    # Convert to returns (% of capital at start of day)
    capital = risk_manager.config.total_capital
    dates = sorted(daily_pnl.keys())
    returns_data = {pd.Timestamp(d): daily_pnl[d] / capital for d in dates}
    returns = pd.Series(returns_data)
    returns.index = pd.DatetimeIndex(returns.index)

    tearsheet = generate_tearsheet(returns, output_path="logs/pyfolio_report.html")

    logger.info(
        f"[ADVANCED_RISK] Tearsheet: "
        f"Sharpe={tearsheet.get('sharpe', 0):.3f} "
        f"Sortino={tearsheet.get('sortino', 0):.3f} "
        f"MaxDD={tearsheet.get('max_drawdown', 0):.1%} "
        f"Calmar={tearsheet.get('calmar', 0):.3f} "
        f"Ann.Ret={tearsheet.get('annual_return', 0):.1%} "
        f"Ann.Vol={tearsheet.get('annual_vol', 0):.1%}"
    )

    return tearsheet
