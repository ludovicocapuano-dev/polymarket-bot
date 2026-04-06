#!/usr/bin/env python3
"""
Polymarket Multi-Strategy Trading Bot
=======================================

Combina tre strategie documentate per massimizzare il profitto:
1. Arbitraggio Combinatorio ($40M documentati nel 2024-2025)
2. Data-Driven Prediction (74% win rate documentato)
3. Event-Driven Trading (mercati macro e politici)

Uso:
    python bot.py                # Paper trading (simulazione)
    python bot.py --live         # Trading reale (ATTENZIONE!)

Il bot scansiona continuamente Polymarket per opportunita',
le valuta con il risk manager, e piazza ordini automaticamente.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

import requests

from config import Config
from utils.polymarket_api import PolymarketAPI
from utils.binance_feed import BinanceFeed
from utils.betfair_feed import BetfairFeed
from utils.dashboard import Dashboard
from utils.weather_feed import WeatherFeed
from utils.arbbets_feed import ArbBetsFeed
from utils.lunarcrush_feed import LunarCrushFeed
from utils.cryptoquant_feed import CryptoQuantFeed
from utils.finlight_feed import FinlightFeed
from utils.gdelt_feed import GDELTFeed
from utils.glint_feed import GlintFeed
from utils.twitter_feed import TwitterFeed
from utils.nansen_feed import NansenFeed
from utils.dome_feed import DomeFeed
from utils.polymarket_ws_feed import PolymarketWSFeed
from utils.vpin_monitor import VPINMonitor
from utils.risk_manager import RiskManager
from utils.redeemer import Redeemer
from utils.onchain_monitor import OnChainMonitor
from utils.telegram_notifier import TelegramNotifier
from utils.metaculus_feed import CrossPlatformFeed
from utils.perplexity_feed import PerplexityFeed

from strategies.arbitrage import ArbitrageStrategy
from strategies.data_driven import DataDrivenStrategy
from strategies.event_driven import EventDrivenStrategy
from strategies.crypto_5min import Crypto5MinStrategy
from strategies.weather import WeatherStrategy
from strategies.arb_gabagool import GabagoolStrategy
from strategies.high_prob_bond import HighProbBondStrategy
from strategies.market_making import MarketMakingStrategy
from strategies.whale_copy import WhaleCopyStrategy
from strategies.resolution_sniper import ResolutionSniperStrategy
from strategies.negrisk_arb import NegRiskArbScanner
from strategies.holding_rewards import HoldingRewardsStrategy
from strategies.favorite_longshot import FavoriteLongshotStrategy
from strategies.btc_latency import BTCLatencyStrategy
from strategies.sport_latency import SportLatencyStrategy
from strategies.liquidity_vacuum import LiquidityVacuumStrategy
from strategies.abandoned_position import AbandonedPositionStrategy
from strategies.cross_platform_arb import CrossPlatformArbStrategy
from strategies.econ_release_sniper import EconReleaseSniper
from strategies.crowd_sport import CrowdSportStrategy
from strategies.crowd_prediction import CrowdPredictionStrategy
from strategies.mro_kelly import MROKellyStrategy
from strategies.xgboost_predictor import XGBoostStrategy
from utils.uma_monitor import UmaMonitor
from monitoring.quant_metrics import evaluate_all_strategies
from monitoring.hrp import HRPAllocator
from monitoring.kyle_lambda import KyleLambdaEstimator
from utils.kalman_forecast import WeatherKalmanFilter
from utils.advanced_risk import run_advanced_risk_analysis
from utils.horizon_client import HorizonClient, ExecutionResult as HorizonExecResult
from utils.fast_executor import FastExecutor
from utils.logit_market_maker import optimal_quotes, implied_belief_vol, two_sided_quotes
from utils.unusual_whales import UnusualWhalesClient
from utils.uw_polymarket_matcher import UWPolymarketMatcher
from utils.market_db import db as market_db

try:
    from finbert_feed import FinBERTFeed
except ImportError:
    FinBERTFeed = None

# v9.0: Architettura agentica a 6 layer
from validators.signal_validator import SignalValidator, ValidationResult
from validators.devils_advocate import DevilsAdvocate
from validators.signal_converter import (
    from_event_opportunity, from_bond_opportunity,
    from_whale_opportunity, from_prediction, from_weather_opportunity,
)
from monitoring.attribution import AttributionEngine
from monitoring.drift_detector import DriftDetector
from monitoring.calibration import CalibrationEngine
from monitoring.empirical_kelly import EmpiricalKelly
from monitoring.meta_labeler import MetaLabeler
from risk.correlation_monitor import CorrelationMonitor
from risk.tail_risk import TailRiskAgent
from agents.orchestrator import OrchestratorAgent
from execution.execution_agent import ExecutionAgent

# ── Logging ──────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)

def setup_logging(level: str):
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                f"logs/bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            ),
        ],
        force=True,
    )

logger = logging.getLogger("bot")

# ── Banner ───────────────────────────────────────────────────────

BANNER = """
╔═══════════════════════════════════════════════════════════════════════╗
║     Polymarket Multi-Strategy Trading Bot v9.0.0                     ║
║     6-Layer Agentic Architecture | Signal Validator + Devil's Adv.    ║
║     Attribution + Drift + Calibration | Correlation + Tail Risk      ║
║     Orchestrator + Event Bus | TWAP Execution | PG + Redis Storage   ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

# ── Dashboard ────────────────────────────────────────────────────

def dashboard(risk: RiskManager, paper: bool, cycle: int,
              unrealized_pnl: float = None, usdc_balance: float = None,
              real_portfolio: dict = None):
    s = risk.status
    mode = "PAPER" if paper else "LIVE"
    spnl = " | ".join(f"{k}=${v:+.2f}" for k, v in s["strategy_pnl"].items())

    # Mostra stato per strategia
    strat_status = []
    strat_halted = s.get("strategy_halted", {})
    for name in ["high_prob_bond", "arb_gabagool", "weather", "arbitrage", "data_driven", "event_driven", "whale_copy"]:
        if strat_halted.get(name, False):
            strat_status.append(f"{name}: HALT")
        else:
            strat_status.append(f"{name}: OK")
    strat_line = " | ".join(strat_status)

    # v5.8: USDC e unrealized PnL
    usdc_str = f"${usdc_balance:>10,.2f}" if usdc_balance is not None else "        --"
    upnl_str = f"${unrealized_pnl:>+8,.2f}" if unrealized_pnl is not None else "      --"

    # v10.8.4: Portfolio reale (deposito vs valore attuale)
    if real_portfolio:
        rp = real_portfolio
        real_line = (
            f"  REALE:    dep=${rp['deposited']:>8,.2f}  |  PnL=${rp['real_pnl']:>+8,.2f} ({rp['real_pnl_pct']:+.1f}%)"
            f"\n  Cash: ${rp['usdc_cash']:>8,.2f}  |  Posizioni: ${rp['positions_value']:>8,.2f}  |  Totale: ${rp['portfolio_value']:>8,.2f}"
            f"\n  Attive: {rp['n_active']:>3d} | Redeemable: {rp['n_redeemable']} (${rp['redeemable_value']:.2f})"
        )
    else:
        real_line = "  REALE:           --  |  PnL:       --"

    print(f"""
{'=' * 65}
  [{mode}] Ciclo #{cycle} | {datetime.now().strftime('%H:%M:%S')}
{'─' * 65}
  Capitale: ${s['capital']:>10,.2f}  |  PnL oggi: ${s['daily_pnl']:>+8,.2f}
  USDC:     {usdc_str}  |  Unrealized: {upnl_str}
{real_line}
  Trades:   {s['total_trades']:>10d}  |  Win rate: {s['win_rate']:>7.1f}%
  Aperte:   {s['open']:>10d}  |  W/L: {s['wins']}/{s['losses']}
  Esposto:  ${s['exposed']:>9,.2f}  |  Disponibile: ${s['available']:>8,.2f}  |  Floor: ${s['reserve_floor']:>8,.2f}
{'─' * 65}
  PnL strategia: {spnl}
  Stato:         {strat_line}
{'─' * 65}
  {'*** ' + s['halt_reason'] + ' ***' if s['halted'] else 'Stato generale: ATTIVO'}
{'=' * 65}
""")


# ── Bot Principale ───────────────────────────────────────────────

class MultiStrategyBot:

    def __init__(self, config: Config):
        self.config = config
        self.api = PolymarketAPI(config.creds)
        self.binance = BinanceFeed()
        self.betfair = BetfairFeed()  # v12.10: Betfair Exchange streaming for sport latency
        # self.telegram initialized later as TelegramNotifier (line ~510)
        self.dashboard = Dashboard()     # v12.10: Rich terminal dashboard (--dashboard flag)
        self.weather_feed = WeatherFeed()
        self.arbbets_feed = ArbBetsFeed()
        self.lunar_feed = LunarCrushFeed()
        self.cquant_feed = CryptoQuantFeed()
        self.finlight_feed = FinlightFeed()
        self.gdelt_feed = GDELTFeed()
        self.glint_feed = GlintFeed()
        self.twitter_feed = TwitterFeed()
        self.nansen_feed = NansenFeed()
        self.dome_feed = DomeFeed()
        self.ws_feed = PolymarketWSFeed()
        self.vpin_monitor = VPINMonitor()  # v9.2.1: VPIN toxic flow detection
        self.risk = RiskManager(config.risk)

        # Ricarica posizioni aperte dal disco (sopravvive ai restart)
        # v5.6: max_age=96h — serve per POSITION-MGR che vende posizioni >48h
        # (prima era 6h e il MGR non vedeva mai le posizioni vecchie!)
        self.risk.load_open_positions(max_age_hours=96.0)

        # v11.1: Sync con portfolio reale on-chain — il risk manager
        # deve sapere quante posizioni ci sono DAVVERO per non superare i limiti
        funder = config.creds.funder_address.strip()
        if funder:
            self.risk.sync_onchain_positions(funder)

        max_pos = config.risk.max_open_positions
        real_count = len(self.risk.open_trades) + self.risk._onchain_position_count
        if real_count > max_pos * 0.9:
            logger.warning(
                f"[STARTUP] {real_count} posizioni reali "
                f"({len(self.risk.open_trades)} tracciate + "
                f"{self.risk._onchain_position_count} on-chain) "
                f"— vicino/oltre il limite {max_pos}"
            )

        # Imposta budget per strategia
        self.risk.set_strategy_budget("arb_gabagool", config.capital_for("arb_gabagool"))
        # v7.0: crypto_5min DISABILITATO (allocazione 0%)
        # self.risk.set_strategy_budget("crypto_5min", config.capital_for("crypto_5min"))
        self.risk.set_strategy_budget("weather", config.capital_for("weather"))
        self.risk.set_strategy_budget("arbitrage", config.capital_for("arbitrage"))
        self.risk.set_strategy_budget("data_driven", config.capital_for("data_driven"))
        self.risk.set_strategy_budget("event_driven", config.capital_for("event_driven"))

        # Inizializza strategie
        # v7.0: crypto_5min DISABILITATO (Kelly negativo, fees 3.15% > edge)
        # self.crypto5 = Crypto5MinStrategy(...)

        # v12.0.1: Meta-Labeler (Lopez de Prado AFML Ch 3)
        self.meta_labeler = MetaLabeler()
        self.meta_labeler.load()

        self.weather = WeatherStrategy(
            self.api, self.risk, self.weather_feed,
            min_edge=0.03,  # v5.3: 3% (same-day 1.8% via 0.6x) + Bayesian posterior
            meta_labeler=self.meta_labeler,  # v12.0.1: Lopez de Prado meta-labeling
        )
        self.arb = ArbitrageStrategy(
            self.api, self.risk,
            arbbets=self.arbbets_feed,
            dome=self.dome_feed,
            min_edge=config.risk.min_edge,
        )
        self.data = DataDrivenStrategy(
            self.api, self.risk, self.binance,
            lunar=self.lunar_feed,
            cquant=self.cquant_feed,
            nansen=self.nansen_feed,
            min_edge=config.risk.min_edge,
        )
        self.event = EventDrivenStrategy(
            self.api, self.risk,
            finlight=self.finlight_feed,
            gdelt=self.gdelt_feed,
            glint=self.glint_feed,
            twitter=self.twitter_feed,
            min_edge=config.risk.min_edge,
        )
        self.gabagool = GabagoolStrategy(
            self.api, self.risk,
            min_profit=0.005,  # 0.5% profitto minimo per trade
        )

        # v6.0: Nuove strategie
        self.bond = HighProbBondStrategy(
            self.api, self.risk,
            min_edge=0.01,  # 1% — bond near-certain
        )
        self.risk.set_strategy_budget("high_prob_bond", config.capital_for("high_prob_bond"))

        # v12.3: Market Making riattivato con v2.0 two-sided quoting
        from strategies.market_making import MarketMakingStrategy
        self.mm = MarketMakingStrategy(self.api, self.risk)
        self.risk.set_strategy_budget("market_making", 500.0)  # budget indipendente

        self.whale = WhaleCopyStrategy(
            self.api, self.risk,
            min_edge=0.03,
        )
        self.risk.set_strategy_budget("whale_copy", config.capital_for("whale_copy"))

        # v10.4: On-Chain Monitor — rilevamento whale trade via Polygon WebSocket (~2s)
        from strategies.whale_copy import TRACKED_WALLETS as _TW
        tracked_addrs = {v["address"] for v in _TW.values() if v.get("address")}
        self.onchain_monitor = OnChainMonitor(tracked_wallets=tracked_addrs)
        self.onchain_monitor.add_callback(self.whale.on_chain_trade)

        # v5.9.2: Resolution Sniper + v10.8: Perplexity verification
        self.uma_monitor = UmaMonitor()
        self.perplexity_feed = PerplexityFeed()
        self.sniper = ResolutionSniperStrategy(
            self.api, self.risk,
            uma_monitor=self.uma_monitor,
            perplexity=self.perplexity_feed,
            min_edge=0.03,  # 3% — fee-free markets
        )
        self.risk.set_strategy_budget("resolution_sniper", config.capital_for("resolution_sniper"))

        # ── v13.1: Horizon SDK — PRIMARY execution engine ──
        self.horizon = HorizonClient()
        if self.horizon.connect():
            logger.info(f"[HORIZON] Connected as primary execution — {self.horizon.status()}")
        else:
            logger.info("[HORIZON] Running without Horizon (native execution only)")
        # Wire native API as fallback for when Horizon is unavailable or errors
        self.horizon.set_native_fallback(self.api.smart_buy, self.api.smart_sell)
        # Wire Horizon into strategies that were initialized before it
        self.weather.horizon = self.horizon

        # ── v14.0: FastExecutor — pre-signed orders for sub-100ms execution ──
        self.fast_executor = None
        if self.api.clob:
            self.fast_executor = FastExecutor(self.api.clob)
            logger.info(f"[FAST-EXEC] Initialized — {self.fast_executor.status_str()}")
        else:
            logger.info("[FAST-EXEC] Skipped — CLOB client not authenticated yet")

        # ── v12.6: Unusual Whales — congress + darkpool + insider signals ──
        self.unusual_whales = UnusualWhalesClient()
        if self.unusual_whales.api_key:
            logger.info("[UW] Unusual Whales client initialized")

        # ── v12.7: UW-Polymarket Matcher — connect UW signals to tradeable markets ──
        self.uw_matcher = UWPolymarketMatcher()
        logger.info("[UW-MATCH] Matcher initialized")

        # ── v10.8.4: NegRisk Sum Arbitrage Scanner ──
        self.negrisk_arb = NegRiskArbScanner()

        # ── v12.5: Economic Data Release Sniper ──
        self.econ_sniper = EconReleaseSniper(self.api, self.risk)
        self.econ_sniper.fetch_schedule()
        nxt = self.econ_sniper.next_release()
        if nxt:
            logger.info(f"[ECON-SNIPER] Next release: {nxt.name} on {nxt.date.strftime('%Y-%m-%d %H:%M UTC')}")

        # ── v12.6: Crowd Sport — DISABILITATO v12.9.1 (no edge, LLM consensus ≠ informazione) ──
        # self.crowd_sport = CrowdSportStrategy(api=self.api, risk=self.risk)
        # self.risk.set_strategy_budget("crowd_sport", 300.0)
        logger.info("[CROWD-SPORT] DISABILITATO v12.9.1 — capitale riallocato a weather")

        # ── v12.7: Crowd Prediction — DISABILITATO v12.9.1 (no edge, costa $0.08/mercato DeepSeek) ──
        # self.crowd_prediction = CrowdPredictionStrategy(
        #     api=self.api, risk=self.risk,
        #     domains=["politics", "crypto", "geopolitics", "entertainment"],
        # )
        # self.risk.set_strategy_budget("crowd_prediction", 200.0)
        logger.info("[CROWD-PRED] DISABILITATO v12.9.1 — capitale riallocato a weather")

        # ── v10.8.4: Holding Rewards (4% APY) + Favorite-Longshot Bias ──
        self.holding_rewards = HoldingRewardsStrategy()
        self.favorite_longshot = FavoriteLongshotStrategy()
        # v10.8.6: BTC Latency Arb v3.0 — Multi-Mode (Sniper + OFI + Latency)
        self.btc_latency = BTCLatencyStrategy(
            api=self.api, risk=self.risk, binance=self.binance,
            horizon=self.horizon,  # v13.1: Horizon primary execution
            fast_executor=self.fast_executor,  # v14.0: sub-100ms execution
            bankroll=1000.0, base_size=30.0, max_size=50.0,  # v12.10.8: raddoppio sizing (13/13 WR, +$541)
        )
        self.risk.set_strategy_budget("btc_latency", 1000.0)
        # v12.10.5: reset PnL tracker — old halt losses shouldn't block new trades
        self.risk._strategy_pnl["btc_latency"] = 0.0

        # v12.10: Sport Latency — Betfair Exchange odds for in-play sport arbitrage
        self.sport_latency = SportLatencyStrategy(
            api=self.api, risk=self.risk, betfair=self.betfair,
            bankroll=500.0, max_size=100.0,
        )
        self.risk.set_strategy_budget("sport_latency", 500.0)

        # v12.10: Liquidity Vacuum Sniper — mean reversion on thin book spikes
        self.liquidity_vacuum = LiquidityVacuumStrategy(
            api=self.api, risk=self.risk,
        )
        self.risk.set_strategy_budget("liquidity_vacuum", 300.0)

        # v12.9: MRO-Kelly — Mean Reversion Oscillator on BTC 5-min markets
        self.mro_kelly = MROKellyStrategy(
            api=self.api, risk=self.risk, binance=self.binance,
            horizon=self.horizon,  # v13.1: Horizon primary execution
            fast_executor=self.fast_executor,  # v14.0: sub-100ms execution
            max_bet=70.0, min_bet=20.0, min_edge=0.05,  # v12.10.8: raddoppio sizing (13/13 WR, +$541)
            kelly_fraction=0.30, max_open_positions=5,
        )
        self.risk.set_strategy_budget("mro_kelly", 500.0)  # v12.10: budget alzato da $200
        logger.info("[MRO-KELLY] Strategy initialized (v12.10: $10-35/trade, max 5 pos, budget $500)")

        # v13.0: XGBoost Prediction Strategy — 330 boosted stumps, 7 features
        self.xgboost_strategy = XGBoostStrategy(
            api=self.api, risk=self.risk,
            max_bet=60.0, min_bet=5.0, kelly_fraction=0.25,
            max_open_positions=15,
        )
        self.risk.set_strategy_budget("xgboost_pred", 300.0)
        logger.info("[XGBOOST] Strategy initialized (budget=$300, max_bet=$60, 7-feature model)")

        # v12.0: Book-derived strategies
        self.abandoned_position = AbandonedPositionStrategy()
        self.cross_platform_arb = CrossPlatformArbStrategy(
            cross_platform_feed=self.cross_platform_feed if hasattr(self, 'cross_platform_feed') else None
        )

        # v12.0: Quant monitoring (Lopez de Prado)
        active_strats = ["weather", "resolution_sniper", "favorite_longshot",
                         "holding_rewards", "negrisk_arb", "btc_latency", "mro_kelly",
                         "xgboost_pred"]
        self.hrp_allocator = HRPAllocator(strategy_names=active_strats)
        self.kyle_lambda = KyleLambdaEstimator()
        self.kalman_filter = WeatherKalmanFilter()

        # ── Auto-Redeem vincite risolte ──
        priv_key = config.creds.private_key.strip()
        funder = config.creds.funder_address.strip() if config.creds.funder_address else ""
        self.redeemer = Redeemer(priv_key, funder) if funder else None
        if self.redeemer and self.redeemer.available:
            logger.info("Auto-redeem attivo (web3 connesso a Polygon)")
            # v10.4: Auto-approve USDC per i 3 contratti CLOB
            try:
                approve_results = self.redeemer.check_and_approve_usdc()
                if approve_results:
                    approved = sum(1 for v in approve_results.values() if v == "approved")
                    ok = sum(1 for v in approve_results.values() if v == "ok")
                    failed = sum(1 for v in approve_results.values() if v not in ("ok", "approved"))
                    logger.info(
                        f"[APPROVE] USDC allowance check: {ok} OK, {approved} approvati, {failed} falliti"
                    )
            except Exception as e:
                logger.warning(f"[APPROVE] Errore auto-approve USDC: {e}")
        elif self.redeemer:
            logger.warning("Auto-redeem disabilitato (web3 non disponibile)")

        self._running = False
        self._cycle = 0
        self._real_portfolio = None
        self._usdc_balance = 0.0
        self._resolve_last_check = 0.0
        self._resolved_cache: set[str] = set()
        self._shared_markets_cache: list = []
        self._weather_extra_cache: list = []  # v10.8: cache async weather markets
        self._weather_extra_last: float = 0.0
        self._weather_priority_scan: bool = False  # v10.8.5: latency hunter flag
        self._darwinian_weights: dict[str, float] = {}  # v12.2: Darwinian budget weighting

        # ── v9.0: Layer 2 — Signal Validator + Devil's Advocate ──
        self.devils_advocate = DevilsAdvocate(risk_manager=self.risk)
        self.xplatform_feed = CrossPlatformFeed()  # v10.8: Manifold/Metaculus sanity check
        self.signal_validator = SignalValidator(
            devil_advocate=self.devils_advocate,
            vpin_monitor=self.vpin_monitor,  # v9.2.1: gate #8 VPIN toxic flow
            xplatform_feed=self.xplatform_feed,  # v10.8: gate #9 cross-platform
        )

        # ── v9.0: Layer 5 — Monitoring & Attribution ──
        self.attribution = AttributionEngine()
        self.drift_detector = DriftDetector()

        # v10.0: Empirical Kelly — data-driven position sizing
        self.empirical_kelly = EmpiricalKelly()
        self.risk.empirical_kelly = self.empirical_kelly

        self.calibration = CalibrationEngine(
            self.attribution, self.drift_detector,
            empirical_kelly=self.empirical_kelly,
        )

        # ── v9.0: Layer 3 — Correlation Monitor & Tail Risk ──
        self.correlation_monitor = CorrelationMonitor(risk_manager=self.risk)
        self.risk.meta_labeler = self.meta_labeler  # v12.0.1: meta-labeling callback

        self.risk.correlation_monitor = self.correlation_monitor
        self.risk.drift_detector = self.drift_detector  # v11.0: dynamic σ
        self.risk.ws_feed = self.ws_feed  # v9.2.1: flash move protection
        self.risk.vpin_monitor = self.vpin_monitor  # v9.2.1: VPIN toxic flow
        self.ws_feed.on_trade = self.vpin_monitor.record_trade  # v9.2.1: feed VPIN
        self.tail_risk = TailRiskAgent(risk_manager=self.risk)

        # ── v9.0: Layer 1 — Orchestrator ──
        self.orchestrator = OrchestratorAgent()

        # ── v9.0: Layer 4 — Execution Engine ──
        # v10.2 TODO: route trade >0 through execution_agent.execute_plan() for TWAP
        self.execution_agent = ExecutionAgent(api=self.api)

        # ── v9.0: Layer 0 — Storage (graceful: se non configurato, noop) ──
        self.db = None
        self.event_bus = None
        if config.db_dsn:
            try:
                from storage.database import Database
                self.db = Database(config.db_dsn)
                self.db.connect()
                self.risk.db = self.db
                self.attribution.db = self.db
                self.drift_detector.db = self.db
                self.calibration.db = self.db
                logger.info("[v9.0] PostgreSQL storage attivo")
            except Exception as e:
                logger.warning(f"[v9.0] PostgreSQL non disponibile: {e}")
        if config.redis_url:
            try:
                from storage.redis_bus import EventBus
                self.event_bus = EventBus(config.redis_url)
                self.event_bus.connect()
                self.orchestrator.event_bus = self.event_bus
                logger.info("[v9.0] Redis event bus attivo")
            except Exception as e:
                logger.warning(f"[v9.0] Redis non disponibile: {e}")

        # v10.8: Telegram notifier per alerting real-time
        self.telegram = TelegramNotifier()

    async def start(self):
        print(BANNER)

        if not self.config.paper_trading:
            logger.info("Autenticazione Polymarket...")
            if not self.api.authenticate():
                logger.error("Autenticazione fallita!")
                sys.exit(1)

        # v12.0.1: Reconcile capital = USDC balance + tracked exposure
        # v13.2: salva USDC reale per Telegram (prima del reconcile che gonfia)
        self._startup_usdc = 0
        try:
            usdc_bal = self.api.get_usdc_balance()
            if usdc_bal >= 0:
                self._startup_usdc = usdc_bal
                real_capital = usdc_bal + self.risk.total_exposed
                if abs(real_capital - self.risk.capital) > 50:
                    logger.info(
                        f"[CAPITAL-RECONCILE] {self.risk.capital:.2f} → {real_capital:.2f} "
                        f"(USDC={usdc_bal:.2f} + exposed={self.risk.total_exposed:.2f})"
                    )
                    self.risk.capital = real_capital
        except Exception as e:
            logger.debug(f"[CAPITAL-RECONCILE] Skip: {e}")

        paper = self.config.paper_trading
        logger.info(f"Bot avviato in modalita' {'PAPER' if paper else 'LIVE'}")
        hz_status = self.horizon.status()
        logger.info(
            f"[EXECUTION] Engine: {'HORIZON v' + str(hz_status.get('version', '?')) if hz_status['connected'] else 'NATIVE'} "
            f"| TWAP>${hz_status['twap_threshold']:.0f} VWAP>${hz_status['vwap_threshold']:.0f}"
        )
        logger.info(
            f"Allocazione v12.10.5: WEATHER={self.config.allocation.weather}% "
            f"| Indipendenti: mro_kelly($500) btc_latency($500) "
            f"liquidity_vacuum($300) sport_latency($500)"
        )

        # v13.2: Notify startup via Telegram — only ACTIVE strategies
        all_strats = [
            f"weather (90%, subtraction mode)",
            f"mro_kelly (${self.mro_kelly.max_bet:.0f}/trade, BTC/ETH/SOL/XRP)",
            f"btc_latency (${self.btc_latency.max_size:.0f}/trade)",
        ]
        # v13.2: usa USDC reale per Telegram, non risk.capital (include exposed vecchio)
        telegram_capital = getattr(self, '_startup_usdc', 0) or self.risk.capital
        await self.telegram.notify_startup(
            mode="PAPER" if paper else "LIVE",
            capital=telegram_capital,
            strategies=all_strats,
        )

        self._running = True

        await asyncio.gather(
            self.binance.connect(),
            self.betfair.connect(),    # v12.10: Betfair Exchange streaming for sport latency
            self.dashboard.run(),      # v12.10: Rich terminal dashboard (noop if not TTY)
            self.uma_monitor.start(),  # v5.9.2: UMA resolution monitor
            self.ws_feed.connect(),    # v9.2: Polymarket WS price feed
            self.onchain_monitor.start(),  # v10.4: On-chain whale trade monitor
            self.glint_feed.connect(),  # v10.5: Glint.trade real-time intelligence
            self._weather_fetch_loop(),  # v10.8: async weather market fetch
            self._model_update_loop(),  # v10.8.5: latency hunter — scan on model update
            self._main_loop(),
            self._dashboard_loop(),
        )

    async def _main_loop(self):
        # Attendi almeno un feed Binance
        while self._running and not self.binance.ready_symbols():
            await asyncio.sleep(0.5)

        ready = self.binance.ready_symbols()
        logger.info(
            f"Feed Binance pronto: {len(ready)} simboli "
            f"({', '.join(s.upper() for s in ready)}) | "
            f"{self.binance.prices_summary()}"
        )

        while self._running:
            try:
                self._cycle += 1
                paper = self.config.paper_trading

                # v12.0.4: reload auto-optimized params every 100 cycles
                if self._cycle % 100 == 1:
                    self._reload_auto_optimized_params()

                # v12.0.5: refresh dynamic city blacklist (learns from outcomes)
                if self._cycle % 200 == 1:
                    try:
                        from weather import refresh_city_blacklist
                        refresh_city_blacklist()
                    except Exception:
                        pass

                # ── v9.2: Fetch ibrido REST + WebSocket ──
                # v13.3: Gamma API DISABILITATA — stalla e blocca main loop.
                # BTC latency e MRO usano CLOB API diretto per market discovery.
                # shared_markets vuoto = orchestrator/validator non attivi (OK).
                shared_markets = getattr(self, '_shared_markets_cache', None) or []

                # ── v9.0: Orchestrator — prioritizzazione mercati ──
                try:
                    orch_tasks = await self.orchestrator.prioritize(shared_markets)
                    # v10.2: Enforce orchestrator routing — rimuovi SKIP markets
                    if hasattr(self, '_priority_map') and self._priority_map:
                        shared_markets = [
                            m for m in shared_markets
                            if self._priority_map.get(m.id, 'LOW') != 'SKIP'
                        ]
                    if orch_tasks:
                        # Classifica temi per il correlation monitor
                        for task in orch_tasks:
                            for m in shared_markets:
                                if m.id == task.market_id:
                                    theme = self.correlation_monitor.classify_theme(
                                        m.id, m.question, m.category, m.tags
                                    )
                                    break
                except Exception as e:
                    logger.debug(f"[ORCHESTRATOR] Errore: {e}")

                # ── Balance check (ogni 50 cicli) ──
                # Il bot deve sapere se ha abbastanza USDC per comprare.
                # Se il saldo e' < $2, salta tutti i trade (ma continua a scannare)
                if self._cycle % 50 == 1 and not self.config.paper_trading:
                    try:
                        usdc_bal = self.api.get_usdc_balance()
                        if usdc_bal >= 0:
                            self._usdc_balance = usdc_bal
                            if usdc_bal < 2.0:
                                logger.warning(
                                    f"[BALANCE] USDC disponibile: ${usdc_bal:.2f} — "
                                    f"troppo basso per tradare, skip buying. "
                                    f"Posizioni aperte: {len(self.risk.open_trades)}"
                                )
                            else:
                                logger.info(f"[BALANCE] USDC disponibile: ${usdc_bal:.2f}")
                        else:
                            logger.warning(f"[BALANCE] API ha ritornato errore (val={usdc_bal})")
                    except Exception as e:
                        logger.warning(f"[BALANCE] Errore check: {e}")

                # v12.8: Auto-redeem ogni 50 cicli — riscuoti vincite automaticamente
                if self._cycle % 50 == 25 and not self.config.paper_trading and self.redeemer:
                    try:
                        redeem_result = await asyncio.to_thread(
                            self.redeemer.redeem_all_redeemable
                        )
                        if redeem_result and redeem_result.get("redeemed", 0) > 0:
                            logger.info(
                                f"[AUTO-REDEEM] Riscossi {redeem_result['redeemed']} posizioni"
                            )
                    except Exception as e:
                        logger.debug(f"[AUTO-REDEEM] Errore: {e}")

                # Skip trading se saldo troppo basso (ma auto-close continua!)
                _can_trade = getattr(self, '_usdc_balance', 999) >= 2.0

                # ── NOTA: ogni strategia ha il suo try/except isolato ──
                # Se una strategia crasha, le altre continuano a funzionare.
                # Prima di v4.0.1, un errore in weather bloccava arb+data+event.

                # ── 0. GABAGOOL Arbitraggio Puro (priorita' MASSIMA — profitto garantito) ──
                # ── GABAGOOL — PAUSED v13.1: focus su mro_kelly + btc_latency + weather ──
                pass

                # ── 0.5. Resolution Sniper — DISABILITATO v12.10.5 ──
                # Postmortem: 0/3 WR, -$97. LLM confidence non calibrata.
                # Perplexity "85% confident" ha perso tutti e 3 i trade.
                pass

                # ── 0.6. NegRisk Sum Arbitrage — PAUSED v12.9 ──
                # 112K wallet study: specialize in 1-2 categories. Arb is noise.
                pass

                # ── 0.7. Holding Rewards — PAUSED v12.9 ──
                # 112K wallet study: focus on 1-2 categories. Holding rewards is noise.
                # if self._cycle % 10 == 0: ...
                pass

                # ── 0.8. Favorite-Longshot Bias — PAUSED v12.9 ──
                # 112K wallet study: 0% WR, -$742 total. Focus on weather instead.
                # if self._cycle % 5 == 0: ...
                pass

                # ── 0.8.6. Economic Data Release Sniper — DISABILITATO v12.10.5 ──
                # Postmortem: 0 trade eseguiti, nessun edge provato. Focus su crypto.
                pass

                # ── 0.8.7. Crowd Sport — DISABILITATO v12.9.1 ──
                # ── 0.8.8. Crowd Prediction — DISABILITATO v12.9.1 ──

                # ── 0.8.5. Market Making v2.0 — DISABILITATO v12.10.5 ──
                # Postmortem: richiede $2K+ budget, 0 trade eseguiti. Focus su crypto arb.
                pass

                # ── 0.8.9. FastExecutor cache warming (every 100 cycles ~50min) ──
                if self.fast_executor and self._cycle % 100 == 1:
                    try:
                        # Warm cache for active crypto market tokens
                        _warm_tokens = set()
                        for strat in [self.btc_latency, self.mro_kelly]:
                            if hasattr(strat, '_active_tokens'):
                                _warm_tokens.update(strat._active_tokens)
                            # Also check recently traded markets for token IDs
                            if hasattr(strat, '_recently_traded'):
                                for mid in list(strat._recently_traded.keys())[:10]:
                                    # Markets have yes/no tokens — we need the actual token_ids
                                    pass
                        if _warm_tokens:
                            self.fast_executor.warm_cache_batch(list(_warm_tokens))
                        self.fast_executor.cleanup_expired()
                    except Exception as e:
                        logger.debug(f"[FAST-EXEC] Cache warm error: {e}")

                # ── 0.9. BTC Latency Arb v3.0 (Multi-Mode) ──
                try:
                    latency_signals = self.btc_latency.scan()
                    if latency_signals:
                        for sig in latency_signals[:2]:  # max 2 per ciclo
                            traded = await self.btc_latency.execute(sig, paper=paper)  # v12.10: live enabled
                            if traded:
                                asyncio.ensure_future(self.telegram.notify_trade("btc_latency", f"BUY {sig.side}", sig.reasoning[:60] if hasattr(sig, 'reasoning') else "BTC 5min", sig.target_size, sig.market_price, sig.edge, paper=paper))
                                self.dashboard.record_trade("btc_latency", f"BUY {sig.side}", sig.target_size, sig.market_price, sig.edge, True, 0, "BTC 5min")
                except Exception as e:
                    logger.warning(f"[BTC-LATENCY] Errore: {e}", exc_info=True)

                # ── 0.9.0.1. Sport Latency — BLOCCATO su richiesta utente ──
                pass

                # ── 0.9.0.2. Liquidity Vacuum — BLOCCATO su richiesta utente ──
                pass

                # ── 0.9.1. MRO-Kelly — Mean Reversion Oscillator BTC 5-min ──
                try:
                    mro_signals = self.mro_kelly.scan()
                    if _can_trade and mro_signals:
                        for sig in mro_signals[:2]:  # max 2 per ciclo
                            if not self._running:
                                break
                            traded = await self.mro_kelly.execute(sig, paper=paper)
                            if traded:
                                asyncio.ensure_future(self.telegram.notify_trade("mro_kelly", f"BUY {sig.side}", sig.reasoning[:60] if hasattr(sig, 'reasoning') else "BTC MRO", sig.target_size, sig.market_price, sig.edge, paper=paper))
                                self.dashboard.record_trade("mro_kelly", f"BUY {sig.side}", sig.target_size, sig.market_price, sig.edge, True, 0, "BTC MRO")
                except Exception as e:
                    logger.warning(f"[MRO-KELLY] Errore: {e}", exc_info=True)

                # ── 0.9.2. XGBoost — DISABILITATO v12.10.5 ──
                # Postmortem: 0 trade, no trained model. Semplificare.
                pass

                # ── 0.10. Abandoned Position Arb — DISABILITATO v12.5.3 ──
                # Motivo: 0% WR, -$742 PnL, posizioni accumulate senza chiudersi
                # I mercati "near-certain" a 95-99c spesso NON risolvono come atteso
                # if _can_trade and aband_opps: ...
                pass

                # ── 0.11. Cross-Platform Arb — DISABILITATO v12.10.5 ──
                # Postmortem: 0 trade, Horizon SDK connesso ma mai tradato. Semplificare.
                pass

                # ── 1. Crypto 5-Min — DISABILITATO v7.0 (fees > edge, Kelly negativo) ──

                # ── 2. Weather (previsioni meteo — mercati giornalieri) ──
                # v13.3: Weather cache async SOSPESA (blocca event loop)
                pass
                # v13.3: Weather SOSPESO — ID scan blocca main loop, impedisce MRO/BTC latency
                # TODO: implementare scan asincrono non-bloccante prima di riattivare
                weather_opps = []
                if False:  # DISABLED
                    if self._weather_priority_scan:
                        self._weather_priority_scan = False
                        logger.info("[LATENCY-HUNTER] Priority weather scan — model update detected")
                try:
                    pass  # weather_opps = await self.weather.scan(shared_markets=shared_markets)
                    if _can_trade:
                        for opp in weather_opps[:5]:  # v10.6: da 3 — più trade weather per ciclo
                            if not self._running:
                                break
                            signal = from_weather_opportunity(opp)
                            price = getattr(signal, 'price', None) or getattr(opp, 'buy_price', 0.5)
                            kelly = self.risk.kelly_size(opp.forecast_prob, price, "weather")
                            report = self.signal_validator.validate(signal, trade_size=kelly)
                            if report.result == ValidationResult.TRADE or (
                                report.result == ValidationResult.REVIEW and signal.edge >= 0.04
                            ):
                                if report.result == ValidationResult.REVIEW:
                                    logger.info(f"[WEATHER] REVIEW accepted: edge={signal.edge:.4f} >= 0.04")
                                self.attribution.record_entry(
                                    opp.market.tokens.get(opp.side.lower(), ""),
                                    "weather", "weather", "weather",
                                    edge_predicted=opp.edge, validation_score=report.score,
                                )
                                traded = await self.weather.execute(opp, paper=paper)
                                if traded:
                                    # v12.9: Get actual fill price for accurate notification
                                    fill_price = getattr(opp, 'buy_price', getattr(opp, 'price', 0.5))
                                    if isinstance(traded, dict) and traded.get("_fill_price"):
                                        fill_price = traded["_fill_price"]
                                    asyncio.ensure_future(self.telegram.notify_trade(
                                        "weather", f"BUY_{opp.side}",
                                        f"{opp.city} {opp.bucket_label}" if hasattr(opp, 'city') else opp.market.question[:60],
                                        opp.target_size if hasattr(opp, 'target_size') else 0,
                                        fill_price,
                                        opp.edge,
                                        paper=paper,
                                    ))
                except Exception as e:
                    logger.error(f"[WEATHER] Errore strategia: {e}", exc_info=True)

                # ── 3. Arbitraggio — PAUSED v13.1: focus su mro + btc + weather ──
                pass

                # ── 4. Data-Driven — PAUSATO v10.6 (WR 42.9% vs break-even 67%, edge hardcoded 0.06) ──
                # Le posizioni aperte vengono gestite dal position manager, ma non apre nuove.
                # try:
                #     predictions = await self.data.analyze(shared_markets=shared_markets)
                #     if _can_trade:
                #         for pred in predictions[:3]:
                #             if not self._running:
                #                 break
                #             signal = from_prediction(pred)
                #             kelly = self.risk.kelly_size(pred.true_prob_yes, signal.price, "data_driven")
                #             report = self.signal_validator.validate(signal, trade_size=kelly)
                #             if report.result == ValidationResult.TRADE:
                #                 self.attribution.record_entry(
                #                     pred.market.tokens.get(pred.best_side.lower(), ""),
                #                     "data_driven", "data_driven", pred.market.category,
                #                     edge_predicted=pred.best_edge, validation_score=report.score,
                #                 )
                #                 await self.data.execute(pred, paper=paper)
                # except Exception as e:
                #     logger.error(f"[DATA] Errore strategia: {e}", exc_info=True)

                # ── 5. Event-Driven — PAUSED v13.1 ──
                pass

                # ── 6. High-Probability Bonds — PAUSED v13.1 ──
                pass

                # ── 7. Market Making — DISABILITATO v7.0 (necessita $2K+ budget) ──

                # ── 8. Whale Copy Trading — PAUSED v13.1 ──
                pass

                # ── 8.1. Whale Profiler (ogni 1000 cicli ~50 min) ──
                if self._cycle % 1000 == 500:
                    try:
                        from utils.whale_profiler import WhaleProfiler
                        profiler = WhaleProfiler()
                        whitelist = profiler.profile_all_wallets()
                        profiler.save_whitelist(whitelist)
                        self.whale._load_whitelist()
                    except Exception as e:
                        logger.warning(f"[WHALE_PROFILER] Errore profiling periodico: {e}")

                # ── 9. Monitoraggio mercati risolti + Auto-Redeem ──
                try:
                    resolved = await self._check_resolved_markets()
                    for r in resolved:
                        for t in list(self.risk.open_trades):
                            if t.market_id == r["market_id"]:
                                if r["won"]:
                                    pnl = t.size * ((1.0 / t.price) - 1.0)
                                else:
                                    pnl = -t.size
                                self.risk.close_trade(t.token_id, won=r["won"], pnl=pnl)
                                # v9.0: Attribution exit + Drift recording
                                self.attribution.record_exit(
                                    t.token_id, pnl=pnl, won=r["won"]
                                )
                                self.drift_detector.record_outcome(
                                    t.strategy, r["won"], pnl=pnl,
                                    city=getattr(t, 'city', ''),
                                )
                                logger.info(
                                    f"[PNL] Trade chiuso: "
                                    f"{'VINTO' if r['won'] else 'PERSO'} "
                                    f"${pnl:+.2f} ({t.strategy}) "
                                    f"redeemed={r.get('redeemed', False)}"
                                )
                                # v10.8: Telegram alert per risoluzione
                                asyncio.ensure_future(self.telegram.notify_resolution(
                                    market_name=r.get("question", t.market_id[:20]),
                                    won=r["won"], pnl=pnl, strategy=t.strategy,
                                ))
                except Exception as e:
                    logger.error(f"[PNL] Errore monitoraggio risolti: {e}", exc_info=True)

                # ── 10. Pulizia ordini stale (ogni 20 cicli) ──
                if self._cycle % 20 == 0 and not self.config.paper_trading:
                    try:
                        stale = self.api.get_open_orders()
                        if stale:
                            logger.info(
                                f"[ORDERS] {len(stale)} ordini aperti residui, "
                                f"cancello stale"
                            )
                            self.api.cancel_all()
                    except Exception as e:
                        logger.debug(f"[ORDERS] Errore pulizia: {e}")

                # ── 10.5. Auto-Scaler crypto sizing (ogni 200 cicli ~100min) ──
                if self._cycle % 200 == 100:
                    self._auto_scale_crypto()

                # ── 11. Purga posizioni stale (ogni 100 cicli) ──
                if self._cycle % 100 == 0:
                    self.risk.purge_stale_positions(max_age_hours=48.0)

                    # v12.1: Cleanup posizioni fantasma (0 shares on-chain)
                    if not self.config.paper_trading and self.risk.open_trades:
                        try:
                            phantom_removed = 0
                            # Check max 20 posizioni per ciclo per non sovraccaricare
                            to_check = list(self.risk.open_trades)[:20]
                            for t in to_check:
                                bal = await asyncio.to_thread(
                                    self.api.get_token_balance, t.token_id
                                )
                                if bal == 0:
                                    self.risk.close_trade(
                                        t.token_id, won=False, pnl=0.0
                                    )
                                    phantom_removed += 1
                                    logger.info(
                                        f"[PHANTOM-CLEANUP] Rimossa: "
                                        f"{t.strategy} '{t.reason[:40]}'"
                                    )
                            if phantom_removed:
                                logger.info(
                                    f"[PHANTOM-CLEANUP] {phantom_removed} "
                                    f"posizioni fantasma rimosse"
                                )
                        except Exception as e:
                            logger.debug(f"[PHANTOM-CLEANUP] Errore: {e}")

                    # v10.4: Position health check (ogni 100 cicli)
                    if not self.config.paper_trading and self.risk.open_trades:
                        try:
                            await self._log_position_health()
                        except Exception as e:
                            logger.debug(f"[HEALTH] Errore: {e}")

                # ── 12. Auto-sell posizioni vecchie (v5.6) ──
                # Ogni 200 cicli (~10 min), vendi posizioni vecchie.
                # v5.7: Se il saldo è basso, vendi le più vecchie (qualunque età)
                # per riciclare capitale. "Emergency capital recovery."
                if self._cycle % 200 == 0 and not self.config.paper_trading:
                    try:
                        usdc = getattr(self, '_usdc_balance', 999)
                        n_open = len(self.risk.open_trades)
                        if usdc < 10.0 and n_open > 5:
                            # EMERGENCY: saldo basso + troppe posizioni →
                            # vendi le 3 più vecchie indipendentemente dall'età
                            logger.warning(
                                f"[POSITION-MGR] EMERGENCY: saldo=${usdc:.2f} con "
                                f"{n_open} posizioni — forzo vendita delle più vecchie"
                            )
                            await self._auto_close_stale_positions(max_age_hours=0.5)
                        else:
                            # Normale: vendi solo posizioni > 24h
                            await self._auto_close_stale_positions(max_age_hours=24.0)
                    except Exception as e:
                        logger.warning(f"[POSITION-MGR] Errore auto-close: {e}")

                # ── 13. PNL-LIVE: calcola unrealized PnL (ogni 100 cicli) ──
                # v5.8.1: spostato da _dashboard_loop a _main_loop
                # perché il dashboard loop gira ogni 20s e "salta" il ciclo esatto.
                if self._cycle % 100 == 0 and self.risk.open_trades and not self.config.paper_trading:
                    try:
                        u_pnl, u_win, u_loss = await self._calc_unrealized_pnl()
                        self._unrealized_pnl = u_pnl
                        self._unrealized_up = u_win
                        self._unrealized_down = u_loss
                        usdc = getattr(self, '_usdc_balance', 0)
                        logger.info(
                            f"[PNL-LIVE] Unrealized: ${u_pnl:+.2f} "
                            f"({u_win} in profitto, {u_loss} in perdita) | "
                            f"USDC libero: ${usdc:.2f} | "
                            f"Posizioni: {len(self.risk.open_trades)}"
                        )
                    except Exception as e:
                        logger.warning(f"[PNL-LIVE] Errore calcolo: {e}")

                # ── v10.8.3: Portfolio reale dalla Data API (al primo ciclo, poi ogni 200) ──
                if self._cycle % 200 == 0 or self._real_portfolio is None:
                    try:
                        portfolio = self._fetch_real_portfolio()
                        if portfolio:
                            self._real_portfolio = portfolio
                            logger.info(
                                f"[PORTFOLIO] REALE: dep=${portfolio['deposited']:.2f} "
                                f"cash=${portfolio['usdc_cash']:.2f} pos=${portfolio['positions_value']:.2f} "
                                f"tot=${portfolio['portfolio_value']:.2f} "
                                f"PnL=${portfolio['real_pnl']:+.2f} ({portfolio['real_pnl_pct']:+.1f}%) "
                                f"| {portfolio['n_active']} attive, {portfolio['n_redeemable']} redeemable"
                            )
                    except Exception as e:
                        logger.warning(f"[PORTFOLIO] Errore: {e}")

                # ── v10.4: P&L Report periodico (ogni 50 cicli) ──
                if self._cycle % 50 == 0 and self._cycle > 0:
                    try:
                        self._log_pnl_report()
                    except Exception as e:
                        logger.debug(f"[PNL-REPORT] Errore: {e}")

                # ── v11.1: Re-sync posizioni on-chain ogni 100 cicli ──
                if self._cycle % 100 == 0 and self._cycle > 0:
                    try:
                        funder = self.config.creds.funder_address.strip()
                        if funder:
                            self.risk.sync_onchain_positions(funder)
                    except Exception as e:
                        logger.debug(f"[SYNC] Errore re-sync: {e}")

                    # v13.1: Horizon position sync — detect untracked positions
                    try:
                        if self.horizon.available:
                            sync_stats = self.horizon.sync_positions_with_risk(self.risk)
                            if sync_stats.get("untracked", 0) > 0:
                                logger.warning(
                                    f"[HORIZON-SYNC] {sync_stats['untracked']} untracked "
                                    f"positions detected on-chain"
                                )
                            logger.info(
                                f"[HORIZON-SYNC] Sync complete: {sync_stats.get('synced', 0)} "
                                f"on-chain, {sync_stats.get('tracked', 0)} tracked"
                            )
                    except Exception as e:
                        logger.debug(f"[HORIZON-SYNC] Errore: {e}")

                    # v13.1: Log Horizon execution stats
                    try:
                        hz_stats = self.horizon.status().get("stats", {})
                        if hz_stats.get("horizon_orders", 0) > 0 or hz_stats.get("native_fallbacks", 0) > 0:
                            logger.info(
                                f"[HORIZON-STATS] orders={hz_stats.get('horizon_orders', 0)} "
                                f"(limit={hz_stats.get('horizon_limit', 0)} "
                                f"twap={hz_stats.get('horizon_twap', 0)} "
                                f"vwap={hz_stats.get('horizon_vwap', 0)}) "
                                f"fallbacks={hz_stats.get('native_fallbacks', 0)} "
                                f"errors={hz_stats.get('errors', 0)}"
                            )
                    except Exception:
                        pass

                # ── v9.0: Drift + Calibration (ogni 500 cicli) ──
                if self._cycle % 500 == 0 and self._cycle > 0:
                    try:
                        drift_alerts = self.drift_detector.check_drift()
                        for alert in drift_alerts:
                            logger.warning(f"[DRIFT] {alert.message}")
                        # v12.0.5: Calibration → azione (auto-apply suggestions)
                        suggestions = self.calibration.analyze()
                        for s in suggestions:
                            logger.info(f"[CALIBRATION] {s.reason}")
                            # Auto-apply min_edge increase for weather
                            if (s.strategy == "weather" and s.parameter == "min_edge"
                                    and "+0.01" in s.new_value):
                                old_edge = self.weather.min_edge
                                self.weather.min_edge = min(0.15, old_edge + 0.01)
                                logger.info(
                                    f"[AUTO-CALIBRATE] weather.min_edge: "
                                    f"{old_edge:.3f} → {self.weather.min_edge:.3f} "
                                    f"(Brier-driven)"
                                )
                            # Auto-apply min_edge decrease
                            elif (s.strategy == "weather" and s.parameter == "min_edge"
                                  and "-0.005" in s.new_value):
                                old_edge = self.weather.min_edge
                                self.weather.min_edge = max(0.02, old_edge - 0.005)
                                logger.info(
                                    f"[AUTO-CALIBRATE] weather.min_edge: "
                                    f"{old_edge:.3f} → {self.weather.min_edge:.3f} "
                                    f"(Brier good)"
                                )

                        # v11.0 + v12.0.5: IC + health + auto-halt on weak signal
                        for strat in ["weather", "resolution_sniper", "favorite_longshot"]:
                            health = self.drift_detector.get_strategy_health(strat)
                            if health["samples"] > 0:
                                ic = self.attribution.get_information_coefficient(strategy=strat)
                                brier_d = self.attribution.get_brier_decomposition(strategy=strat)
                                logger.info(
                                    f"[QUANT] {strat}: H={health['health_score']:.2f} "
                                    f"({health['status']}) IC={ic:.3f} "
                                    f"drift={health['drift_score']:.2f} "
                                    f"Brier={brier_d['brier']:.3f} "
                                    f"(rel={brier_d['reliability']:.3f} "
                                    f"res={brier_d['resolution']:.3f})"
                                )
                                # v12.0.5: Auto-halt on low IC (signal is noise)
                                n_completed = len([
                                    a for a in self.attribution._completed
                                    if a.strategy == strat
                                ])
                                if ic < 0.03 and n_completed >= 50:
                                    logger.warning(
                                        f"[SELF-LEARN] {strat}: IC={ic:.3f} < 0.03 "
                                        f"({n_completed} trades) → HALT 24h"
                                    )
                                    self.risk._strategy_halted[strat] = True
                                    self.risk._strategy_halt_reason[strat] = (
                                        f"IC={ic:.3f} weak signal ({n_completed} trades)"
                                    )
                                    import time as _t
                                    self.risk._strategy_halt_until[strat] = _t.time() + 86400
                    except Exception as e:
                        logger.warning(f"[v9.0] Errore drift/calibration: {e}")

                    # v10.0: Empirical Kelly recalculation
                    try:
                        for strat in ["high_prob_bond", "data_driven", "weather", "event_driven", "whale_copy"]:
                            closed = [t for t in self.risk.trades if t.strategy == strat and t.result in ("WIN", "LOSS")]
                            if self.empirical_kelly.needs_recalc(strat, len(closed), self._cycle):
                                self.empirical_kelly.update(strat, closed, self._cycle)
                    except Exception as e:
                        logger.warning(f"[v10.0] Errore empirical kelly: {e}")

                    # v12.0: PSR + DSR + binHR (Lopez de Prado)
                    try:
                        trades_by_strat = {}
                        for t in self.risk.trades:
                            if t.result in ("WIN", "LOSS") and t.pnl is not None:
                                trades_by_strat.setdefault(t.strategy, []).append(t.pnl)
                        if trades_by_strat:
                            reports = evaluate_all_strategies(trades_by_strat, n_tested=13)
                            for name, r in reports.items():
                                if not r.is_structurally_viable:
                                    logger.warning(
                                        f"[QUANT-RISK] {name} AT RISK: P(fail)={r.prob_failure:.1%} "
                                        f"WR={r.win_rate:.1%} vs BE={r.breakeven_precision:.1%}"
                                    )
                    except Exception as e:
                        logger.warning(f"[v12.0] Errore quant metrics: {e}")

                    # v12.0.1: Meta-Labeler status
                    if self.meta_labeler:
                        ml = self.meta_labeler.status()
                        logger.info(
                            f"[QUANT] MetaLabel: phase={ml['phase']} "
                            f"samples={ml['samples']}/{ml['warm_at']} "
                            f"WR={ml['wr']:.3f} predictions={ml['predictions']}"
                        )

                # ── v9.0: Tail Risk + Portfolio VaR (ogni 200 cicli) ──
                if self._cycle % 200 == 0 and self._cycle > 0:
                    try:
                        tail_report = self.tail_risk.analyze()
                        if tail_report.risk_level == "CRITICAL":
                            logger.warning(
                                f"[TAIL_RISK] CRITICAL: max loss "
                                f"${abs(tail_report.max_loss_scenario):.2f} "
                                f"({tail_report.exposure_pct:.0%} capitale)"
                            )
                        if tail_report.cvar_95 > 0:
                            logger.info(
                                f"[TAIL_RISK] CVaR95=${tail_report.cvar_95:.2f} "
                                f"(VaR95=${tail_report.var_95:.2f})"
                            )
                    except Exception as e:
                        logger.warning(f"[v9.0] Errore tail risk: {e}")
                    # v10.2: Portfolio VaR con matrice di covarianza
                    try:
                        if self.correlation_monitor:
                            pvar = self.correlation_monitor.portfolio_var()
                            if pvar["n_positions"] > 1:
                                logger.info(
                                    f"[PORTFOLIO_VAR] VaR95=${pvar['portfolio_var']:.2f} "
                                    f"diversification={pvar['diversification_ratio']:.2%} "
                                    f"({pvar['n_positions']} pos)"
                                )
                    except Exception as e:
                        logger.warning(f"[v10.2] Errore portfolio VaR: {e}")

                    # v12.2: Auto-Revert — check optimizer params after 72h
                    try:
                        self._check_auto_revert()
                    except Exception as e:
                        logger.debug(f"[AUTO-REVERT] Errore: {e}")

                # ── v12.3: Auto-Compound (ogni 200 cicli ~100 min) ──
                if self._cycle % 200 == 0 and self._cycle > 0 and not self.config.paper_trading:
                    try:
                        self._auto_compound()
                    except Exception as e:
                        logger.debug(f"[AUTO-COMPOUND] Errore: {e}")

                # ── v12.2: Darwinian Reweight (ogni 1000 cicli ~8h) ──
                if self._cycle % 1000 == 0 and self._cycle > 0:
                    try:
                        self._darwinian_reweight()
                    except Exception as e:
                        logger.debug(f"[DARWINIAN] Errore: {e}")

                # ── v12.4: Advanced Risk Analytics (ogni 1000 cicli ~8h) ──
                # GARCH model selection, CVaR allocation, PyFolio tearsheet
                # Logs recommendations only — does NOT auto-apply changes
                if self._cycle % 1000 == 0 and self._cycle > 0:
                    try:
                        adv_report = run_advanced_risk_analysis(self.risk)
                        if adv_report.get("allocation"):
                            alloc = adv_report["allocation"]
                            for method in ["CVaR", "MVO", "HRP"]:
                                if method in alloc and not alloc[method].get("fallback"):
                                    weights = alloc[method].get("weights", {})
                                    top = sorted(weights.items(), key=lambda x: -x[1])[:3]
                                    top_str = ", ".join(f"{k}={v:.0%}" for k, v in top)
                                    logger.info(
                                        f"[ADVANCED_RISK] {method} recommends: {top_str}"
                                    )
                    except Exception as e:
                        logger.debug(f"[ADVANCED_RISK] Errore: {e}")

                # ── v12.6: Unusual Whales scan (ogni 200 cicli ~100 min) ──
                if self._cycle % 200 == 100 and self._cycle > 0:
                    try:
                        uw_signals = self.unusual_whales.scan_all()
                        if uw_signals:
                            for s in uw_signals[:5]:
                                logger.info(
                                    f"[UW] {s.source}: {s.direction} {s.ticker} "
                                    f"strength={s.strength:.2f} | {s.detail[:50]}"
                                )

                            # ── v12.7: Match UW signals to Polymarket markets ──
                            try:
                                actionable = self.uw_matcher.get_actionable(
                                    uw_signals, min_edge=0.05
                                )
                                for opp in actionable[:5]:
                                    logger.info(
                                        f"[UW-MATCH] {opp.signal_source}: "
                                        f"{opp.signal_ticker} → {opp.suggested_side} "
                                        f"on '{opp.market_question[:60]}' "
                                        f"edge={opp.edge_estimate:.1%} "
                                        f"conf={opp.confidence:.2f}"
                                    )
                                if actionable:
                                    logger.info(
                                        f"[UW-MATCH] {len(actionable)} actionable "
                                        f"opportunities found (edge>=5%)"
                                    )
                            except Exception as e:
                                logger.debug(f"[UW-MATCH] Matcher error: {e}")

                    except Exception as e:
                        logger.debug(f"[UW] Scan error: {e}")

                # ── v12.4.1: Hyperspace Sync (ogni 500 cicli ~4h) ──
                if self._cycle % 500 == 250 and self._cycle > 0:
                    try:
                        from hyperspace_optimizer import sync as hyperspace_sync
                        hs_result = hyperspace_sync("weather", auto_adopt=False)
                        if hs_result.get("adopted"):
                            adopted = hs_result["adopted"]
                            logger.info(
                                f"[HYPERSPACE] Peer {adopted['peer_id']} ha params "
                                f"{adopted['improvement']:+.1f}% migliori — flagged per review"
                            )
                        elif hs_result.get("published"):
                            logger.debug(
                                f"[HYPERSPACE] Sync completato — "
                                f"score={hs_result.get('published_score', 0):.4f}"
                            )
                    except Exception as e:
                        logger.debug(f"[HYPERSPACE] Errore sync: {e}")

                # ── v12.4: Market Intelligence (ogni 500 cicli ~4h) ──
                if self._cycle % 500 == 0 and self._cycle > 0:
                    try:
                        from scripts.market_intelligence import run_market_intelligence, save_report
                        market_dicts = [
                            {"question": m.question, "title": m.question}
                            for m in shared_markets
                        ]
                        active_titles = [
                            t.market_title for t in self.risk.open_trades
                            if hasattr(t, "market_title") and t.market_title
                        ]
                        intel_report = run_market_intelligence(
                            markets=market_dicts,
                            active_titles=active_titles,
                            risk_manager=self.risk,
                        )
                        if intel_report.get("n_clusters", 0) > 0:
                            logger.info(
                                f"[INTEL] {intel_report['n_clusters']} correlation clusters "
                                f"({intel_report['weather_markets']} weather markets)"
                            )
                        if intel_report.get("n_uncovered", 0) > 0:
                            logger.info(
                                f"[INTEL] {intel_report['n_uncovered']} uncovered opportunities"
                            )
                        save_report(intel_report)
                    except Exception as e:
                        logger.debug(f"[INTEL] Errore market intelligence: {e}")

                # Salva trade periodicamente
                if self._cycle % 10 == 0:
                    self.risk.save_trades()

                await asyncio.sleep(self.config.poll_interval)

            except Exception as e:
                logger.error(f"Errore ciclo #{self._cycle}: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _check_resolved_markets(self) -> list[dict]:
        """
        v5.9.4: Controlla risoluzione interrogando DIRETTAMENTE i nostri mercati.

        v5.9.4: Accelerato — controlla ogni 30s invece di 2 min,
        e controlla TUTTI i mercati aperti (non solo 10 a rotazione).
        Con max 20 posizioni aperte, 20 chiamate API ogni 30s e' gestibile.
        """
        now = time.time()
        if now - self._resolve_last_check < 30:  # v5.9.4: ogni 30s (era 120s)
            return []
        self._resolve_last_check = now

        open_trades = self.risk.open_trades
        if not open_trades:
            return []

        # Mappa market_id -> trade info
        trade_map = {}  # market_id -> trade
        for t in open_trades:
            if t.market_id not in self._resolved_cache:
                trade_map[t.market_id] = t

        if not trade_map:
            logger.info(
                f"[PNL] Tutti i {len(open_trades)} mercati già in cache risoluzione"
            )
            return []

        # v5.9.4: Controlla TUTTI i mercati aperti (non più solo 10 a rotazione).
        # Con max 20 posizioni e check ogni 30s, è sostenibile per l'API.
        sorted_trades = sorted(trade_map.values(), key=lambda t: t.timestamp)
        market_ids_to_check = [t.market_id for t in sorted_trades]
        logger.info(
            f"[PNL] Check risoluzione: {len(open_trades)} posizioni, "
            f"controllo {len(market_ids_to_check)} mercati"
        )

        results = []

        # ── v9.2.3: Fast-path Data API — 1 chiamata per tutte le posizioni ──
        data_api_resolved_mids = set()
        has_redeemer = self.redeemer and self.redeemer.available
        if has_redeemer:
            try:
                redeemable_positions = await asyncio.to_thread(
                    self.redeemer.fetch_redeemable_positions
                )
                if redeemable_positions:
                    # Build set di conditionId noti dai nostri trade (via Gamma cache)
                    for pos in redeemable_positions:
                        cid = pos.get("conditionId", "") or pos.get("condition_id", "")
                        if not cid:
                            continue

                        # Matcha con trade_map via slug, title, o conditionId
                        matched_mid = None
                        slug = pos.get("slug", "") or pos.get("market_slug", "")
                        title = pos.get("title", "") or pos.get("question", "")

                        for mid, trade in trade_map.items():
                            if mid in data_api_resolved_mids:
                                continue
                            # Match via conditionId se disponibile nel trade
                            t_cid = getattr(trade, "condition_id", "") or getattr(trade, "conditionId", "")
                            if t_cid and t_cid.replace("0x", "") == cid.replace("0x", ""):
                                matched_mid = mid
                                break
                            # Match via slug
                            if slug and hasattr(trade, "slug") and trade.slug == slug:
                                matched_mid = mid
                                break
                            # Match via title
                            t_title = getattr(trade, "question", "") or getattr(trade, "title", "")
                            if t_title and title and t_title.lower() == title.lower():
                                matched_mid = mid
                                break

                        if not matched_mid:
                            continue

                        trade = trade_map[matched_mid]
                        our_side = getattr(trade, 'side', 'BUY_YES')
                        we_bet_yes = "YES" in our_side.upper()

                        # Determina outcome dalla posizione Data API
                        outcome = pos.get("outcome", "") or pos.get("resolution", "")
                        if outcome:
                            res_lower = outcome.lower().strip()
                            resolved_yes = res_lower in ("yes", "y", "1", "true")
                            won = (we_bet_yes and resolved_yes) or (not we_bet_yes and not resolved_yes)
                            resolution_str = f"{outcome} (Data API redeemable)"
                        else:
                            # redeemable=true senza outcome esplicito — assumiamo win
                            won = True
                            resolution_str = "redeemable (Data API)"

                        # Tenta redeem on-chain
                        redeemed = False
                        if won:
                            try:
                                from utils.redeemer import ResolvedPosition
                                rpos = ResolvedPosition(
                                    market_id=matched_mid,
                                    condition_id=cid,
                                    question=title or "?",
                                    outcome=outcome or "redeemable",
                                    won=True,
                                    neg_risk=pos.get("negRisk", pos.get("neg_risk", False)),
                                )
                                redeemed = self.redeemer._redeem_position(rpos)
                            except Exception as e:
                                logger.warning(f"[REDEEM] On-chain fallito (Data API): {e}", exc_info=True)

                        # v10.2.1: Se redeem ritorna None, la condizione non è
                        # ancora risolta on-chain (race condition Data API vs chain).
                        # NON chiudere il trade — riprova al prossimo ciclo.
                        if redeemed is None:
                            logger.info(
                                f"[PNL] Mercato {matched_mid} non ancora risolvibile "
                                f"on-chain, riprovo al prossimo ciclo"
                            )
                            continue

                        self._resolved_cache.add(matched_mid)
                        data_api_resolved_mids.add(matched_mid)

                        results.append({
                            "market_id": matched_mid,
                            "won": won,
                            "resolution": resolution_str,
                            "redeemed": redeemed,
                        })

                        logger.info(
                            f"[PNL] Mercato RISOLTO (Data API): '{title[:60]}' "
                            f"→ {outcome or 'redeemable'} | Noi: {our_side} → "
                            f"{'WIN ✓' if won else 'LOSS ✗'}"
                        )

                    if data_api_resolved_mids:
                        logger.info(
                            f"[PNL] Data API fast-path: {len(data_api_resolved_mids)} mercati risolti"
                        )
            except Exception as e:
                logger.warning(f"[PNL] Data API fast-path errore, fallback a Gamma: {e}")

        # ── Gamma per-market loop (skip mercati già trovati via Data API) ──
        market_ids_to_check = [mid for mid in market_ids_to_check if mid not in data_api_resolved_mids]
        if not market_ids_to_check and results:
            # Tutti trovati via Data API
            wins = sum(1 for r in results if r["won"])
            losses = len(results) - wins
            logger.info(
                f"[PNL] Ciclo risoluzione: {len(results)} trovati "
                f"({wins}W / {losses}L) — tutti via Data API"
            )
            return results

        for mid in market_ids_to_check:
            try:
                # Query Gamma API per mercato specifico
                resp = await asyncio.to_thread(
                    requests.get,
                    f"https://gamma-api.polymarket.com/markets/{mid}",
                    timeout=10,
                )

                if resp.status_code in (404, 422, 500, 502, 503):
                    # Mercato non trovato o errore API — skip senza bloccare
                    logger.debug(f"[PNL] Mercato {mid}: HTTP {resp.status_code}, skip")
                    continue

                resp.raise_for_status()
                m = resp.json()

                # Check se il mercato è chiuso/risolto
                is_closed = m.get("closed", False)
                if not is_closed:
                    continue  # ancora aperto

                # v5.8.2: Polymarket NON usa campo "resolution".
                # Usa outcomePrices: ["1","0"] = YES vinto, ["0","1"] = NO vinto
                outcome_prices_raw = m.get("outcomePrices", "")
                resolution = m.get("resolution", "")  # fallback

                # Parse outcomePrices (può essere stringa JSON o lista)
                yes_won = None
                if outcome_prices_raw:
                    try:
                        if isinstance(outcome_prices_raw, str):
                            prices = json.loads(outcome_prices_raw)
                        else:
                            prices = outcome_prices_raw
                        # prices = ["1", "0"] o ["0", "1"] o prezzi intermedi
                        p_yes = float(prices[0]) if len(prices) > 0 else 0.5
                        p_no = float(prices[1]) if len(prices) > 1 else 0.5
                        if p_yes > 0.95:
                            yes_won = True
                        elif p_no > 0.95:
                            yes_won = False
                        else:
                            # Prezzi intermedi — mercato chiuso ma non risolto
                            logger.debug(
                                f"[PNL] Mercato {mid} chiuso, prezzi intermedi "
                                f"YES={p_yes} NO={p_no} — non ancora risolto"
                            )
                            continue
                    except (ValueError, TypeError) as e:
                        logger.debug(f"[PNL] Parse outcomePrices fallito per {mid}: {e}")

                # Fallback: campo resolution (raro ma possibile)
                if yes_won is None and resolution:
                    res_lower = resolution.lower().strip()
                    yes_won = res_lower in ("yes", "y", "1", "true")
                elif yes_won is None:
                    logger.debug(f"[PNL] Mercato {mid} chiuso ma impossibile determinare vincitore")
                    continue

                # Trovato un mercato RISOLTO!
                trade = trade_map[mid]
                our_side = getattr(trade, 'side', 'BUY_YES')
                we_bet_yes = "YES" in our_side.upper()
                won = (we_bet_yes and yes_won) or (not we_bet_yes and not yes_won)
                resolution_str = f"YES (prices={outcome_prices_raw})" if yes_won else f"NO (prices={outcome_prices_raw})"

                # Tenta redeem on-chain se disponibile
                redeemed = False
                has_redeemer = self.redeemer and self.redeemer.available
                logger.info(
                    f"[REDEEM] won={won} redeemer={has_redeemer} "
                    f"market={mid} conditionId={m.get('conditionId', 'N/A')[:20]}"
                )
                if has_redeemer and won:
                    try:
                        from utils.redeemer import ResolvedPosition
                        pos = ResolvedPosition(
                            market_id=mid,
                            condition_id=m.get("conditionId", ""),
                            question=m.get("question", "?"),
                            outcome="Yes" if yes_won else "No",
                            won=True,
                            neg_risk=m.get("negRisk", False),
                        )
                        redeemed = self.redeemer._redeem_position(pos)
                    except Exception as e:
                        logger.warning(f"[REDEEM] On-chain fallito: {e}", exc_info=True)

                # v10.2.1: Se redeem ritorna None, condizione non risolta on-chain
                if redeemed is None:
                    logger.info(
                        f"[PNL] Mercato {mid} non ancora risolvibile "
                        f"on-chain (Gamma), riprovo al prossimo ciclo"
                    )
                    continue

                self._resolved_cache.add(mid)

                results.append({
                    "market_id": mid,
                    "won": won,
                    "resolution": resolution_str,
                    "redeemed": redeemed,
                })

                logger.info(
                    f"[PNL] Mercato RISOLTO: '{m.get('question', '?')[:60]}' "
                    f"→ {'YES' if yes_won else 'NO'} | Noi: {our_side} → "
                    f"{'WIN ✓' if won else 'LOSS ✗'}"
                )

            except Exception as e:
                logger.warning(f"[PNL] Errore check mercato {mid}: {e}", exc_info=True)
                continue

        if results:
            wins = sum(1 for r in results if r["won"])
            losses = len(results) - wins
            logger.info(
                f"[PNL] Ciclo risoluzione: {len(results)} trovati "
                f"({wins}W / {losses}L)"
            )

        return results

    async def _auto_close_stale_positions(self, max_age_hours: float = 48.0):
        """
        v7.2: Triple-Barrier Exit System per strategia.

        Usa risk.check_barrier() con soglie differenziate per strategia:
        - TAKE_PROFIT: prezzo salito oltre la soglia TP della strategia
        - STOP_LOSS: prezzo sceso oltre la soglia SL della strategia
        - TIME_EXIT: posizione aperta oltre il max holding della strategia
        - Emergency mode (saldo < $10): vendi le piu' in perdita%

        Priorita' di vendita: STOP_LOSS > TIME_EXIT > TAKE_PROFIT
        """
        now = time.time()

        if not self.risk.open_trades:
            logger.info("[POSITION-MGR] Nessuna posizione aperta")
            return

        # ── Analizza TUTTE le posizioni con triple-barrier ──
        to_sell = []
        held = 0

        for trade in self.risk.open_trades:
            age_hours = (now - trade.timestamp) / 3600
            try:
                book = await asyncio.to_thread(self.api.get_order_book, trade.token_id)
                bids = book.get("bids", [])
                current_bid = float(bids[0]["price"]) if bids else 0
            except Exception:
                current_bid = 0

            # v10.5: Book vuoto → HOLD, non vendere (nessuno compra)
            # Previene il loop dove STOP_LOSS triggerato ma sell fallisce
            # perché non ci sono bid, ripetuto ogni ciclo all'infinito.
            if current_bid == 0:
                logger.debug(
                    f"[POSITION-MGR] Bid=0 (book vuoto): {trade.strategy} "
                    f"entry@{trade.price:.4f} ({age_hours:.0f}h) — HOLD"
                )
                held += 1
                continue

            # Calcola PnL percentuale
            if trade.price > 0 and current_bid > 0:
                pnl_pct = (current_bid - trade.price) / trade.price
            else:
                pnl_pct = 0.0

            # v12.1: Trailing stop — aggiorna high-water mark
            if current_bid > trade.high_water_mark:
                trade.high_water_mark = current_bid

            # v8.0+v12.1: Bid sanity check con depth verification.
            # Se bid < 50% entry MA il book ha profondità reale → crash genuino.
            if trade.price > 0:
                bid_ratio = current_bid / trade.price
                if bid_ratio < 0.50:
                    n_bids = len(bids)
                    total_bid_depth = sum(
                        float(b.get("size", 0)) for b in bids[:5]
                    )
                    if n_bids >= 3 and total_bid_depth >= 5.0:
                        logger.info(
                            f"[POSITION-MGR] Crash confermato "
                            f"(depth={n_bids} bids, ${total_bid_depth:.0f}): "
                            f"{trade.strategy} entry@{trade.price:.4f} "
                            f"bid@{current_bid:.4f} — ALLOW EXIT"
                        )
                        # Fall through al check_barrier
                    else:
                        logger.debug(
                            f"[POSITION-MGR] Bid sospetto (thin book): "
                            f"{trade.strategy} entry@{trade.price:.4f} "
                            f"bid@{current_bid:.4f} "
                            f"(ratio={bid_ratio:.1%}, {n_bids} bids, "
                            f"${total_bid_depth:.0f} depth) — HOLD"
                        )
                        held += 1
                        continue

            # v12.1: Trailing stop — se posizione era +15% e torna a break-even
            if trade.price > 0 and trade.high_water_mark > 0:
                hwm_return = (trade.high_water_mark - trade.price) / trade.price
                if hwm_return >= 0.15 and current_bid <= trade.price * 1.01:
                    logger.info(
                        f"[TRAILING-STOP] {trade.strategy} "
                        f"HWM@{trade.high_water_mark:.4f} (+{hwm_return:.1%}) "
                        f"→ bid@{current_bid:.4f} <= entry@{trade.price:.4f} "
                        f"— BREAK-EVEN EXIT"
                    )
                    to_sell.append((trade, current_bid, age_hours, pnl_pct,
                                    0, "STOP_LOSS"))
                    continue

            # v7.2: Triple-barrier check per strategia
            signal = self.risk.check_barrier(trade, current_bid)

            if signal == "HOLD":
                held += 1
                continue

            # Priorita' numerica per ordinamento: STOP_LOSS=0 > TIME_EXIT=1 > TAKE_PROFIT=2
            priority = {"STOP_LOSS": 0, "TIME_EXIT": 1, "TAKE_PROFIT": 2}.get(signal, 1)

            logger.info(
                f"[POSITION-MGR] {signal} {trade.strategy} "
                f"Buy@{trade.price:.4f} Bid@{current_bid:.4f} "
                f"PnL={pnl_pct:+.1%} ({age_hours:.0f}h)"
            )

            to_sell.append((trade, current_bid, age_hours, pnl_pct, priority, signal))

        if not to_sell:
            if held > 0:
                oldest = min(self.risk.open_trades, key=lambda t: t.timestamp)
                oldest_h = (now - oldest.timestamp) / 3600
                logger.info(
                    f"[POSITION-MGR] {held} posizioni HOLD "
                    f"(piu' vecchia: {oldest_h:.1f}h {oldest.strategy})"
                )
            return

        # Ordina per priorita': STOP_LOSS prima, poi TIME_EXIT, poi TAKE_PROFIT
        # A parita' di priorita': perdita peggiore prima
        if max_age_hours < 1:
            # Emergency: ordina per PnL% (vendi prima le piu' in perdita)
            to_sell.sort(key=lambda x: x[3])
        else:
            to_sell.sort(key=lambda x: (x[4], x[3]))

        logger.info(
            f"[POSITION-MGR] {len(to_sell)} da vendere (barrier triggered), "
            f"{held} HOLD"
        )

        # Limiti di vendita per ciclo (slot pressure)
        n_open = len(self.risk.open_trades)
        max_pos = self.risk.config.max_open_positions
        if max_age_hours < 1:
            max_sells = 10  # Emergency
        elif n_open >= max_pos * 0.9:
            max_sells = 8
            logger.info(
                f"[POSITION-MGR] SLOT-PRESSURE: {n_open}/{max_pos} posizioni "
                f"— vendita aggressiva (max {max_sells})"
            )
        elif n_open >= max_pos * 0.7:
            max_sells = 5
        else:
            max_sells = 3

        closed = 0
        for trade, current_bid, age_hours, pnl_pct, _prio, signal in to_sell[:max_sells]:
            try:
                # v12.1: Pre-sell balance check — verifica shares on-chain
                token_balance = await asyncio.to_thread(
                    self.api.get_token_balance, trade.token_id
                )
                if token_balance == 0:
                    # Nessuna share: posizione fantasma, rimuovila dal tracking
                    self.risk.close_trade(
                        trade.token_id, won=False, pnl=0.0
                    )
                    logger.info(
                        f"[POSITION-MGR] Rimossa posizione fantasma "
                        f"(0 shares on-chain): {trade.strategy} "
                        f"'{trade.reason[:40]}'"
                    )
                    closed += 1
                    continue
                elif token_balance < 0:
                    # Errore query balance, skip (riprova prossimo ciclo)
                    logger.debug(
                        f"[POSITION-MGR] Balance check fallito per "
                        f"{trade.token_id[:16]}, skip"
                    )
                    continue

                # v7.4: Recupera shares REALI dal CLOB per evitare over-sell
                fill_info = self.api.get_last_fill(trade.token_id, side="BUY")
                if fill_info and fill_info["fill_size"] > 0:
                    shares = min(fill_info["fill_size"], token_balance)
                    real_entry = fill_info["fill_price"]
                else:
                    shares = token_balance  # usa balance on-chain
                    real_entry = trade.price

                if shares < 1:
                    self.risk.close_trade(trade.token_id, won=False, pnl=-trade.size)
                    logger.info(
                        f"[POSITION-MGR] Rimossa posizione micro "
                        f"({trade.strategy}) ${trade.size:.2f} "
                        f"({age_hours:.0f}h)"
                    )
                    closed += 1
                    continue

                # v13.1: Route SELL through Horizon (with native fallback)
                if self.horizon.available:
                    hz_sell = await asyncio.to_thread(
                        self.horizon.execute_trade,
                        trade.token_id, "SELL",
                        shares * real_entry,  # size in dollars
                        real_entry,
                        trade.strategy,
                    )
                    result = hz_sell.raw_result if hz_sell.success else None
                else:
                    result = await asyncio.to_thread(
                        self.api.smart_sell,
                        trade.token_id, shares,
                        current_price=real_entry,
                        timeout_sec=8.0,
                        fallback_market=True,
                    )

                if result:
                    sell_price = current_bid if current_bid > 0 else real_entry * 0.5
                    pnl = shares * (sell_price - real_entry)

                    self.risk.close_trade(
                        trade.token_id,
                        won=(pnl > 0),
                        pnl=pnl,
                    )

                    # v8.0: Registra stop-loss cooldown per bloccare ri-acquisto
                    if signal == "STOP_LOSS":
                        self.risk.register_stop_loss(trade.market_id)

                    logger.info(
                        f"[POSITION-MGR] {signal} {trade.strategy} "
                        f"'{trade.reason[:30]}' | "
                        f"Buy@{trade.price:.4f} Sell@{sell_price:.4f} "
                        f"PnL=${pnl:+.2f} ({pnl_pct:+.1%}) ({age_hours:.0f}h)"
                    )
                    # v12.5.2: Telegram notification for ALL strategy exits
                    asyncio.ensure_future(self.telegram.notify_resolution(
                        market_name=f"[{signal}] {trade.reason[:40]}",
                        won=(pnl > 0), pnl=pnl, strategy=trade.strategy,
                    ))
                    closed += 1
                else:
                    logger.warning(
                        f"[POSITION-MGR] Sell fallito per {trade.token_id[:16]} "
                        f"({trade.strategy})"
                    )
            except Exception as e:
                logger.warning(
                    f"[POSITION-MGR] Errore close {trade.token_id[:16]}: {e}"
                )

        if closed > 0:
            logger.info(f"[POSITION-MGR] Chiuse {closed}/{len(to_sell)} posizioni")

        # v12.1: Persisti high-water mark aggiornati
        if self.risk.open_trades:
            self.risk._save_open_positions()

    async def _calc_unrealized_pnl(self) -> tuple[float, int, int]:
        """
        v5.8: Calcola PnL non realizzato delle posizioni aperte.
        Controlla il prezzo corrente di ogni posizione e calcola il guadagno/perdita.

        Returns: (total_unrealized_pnl, n_profit, n_loss)
        """
        total_pnl = 0.0
        n_profit = 0
        n_loss = 0
        checked = 0

        for trade in self.risk.open_trades:
            try:
                book = await asyncio.to_thread(self.api.get_order_book, trade.token_id)
                bids = book.get("bids", [])
                asks = book.get("asks", [])

                if bids:
                    current_price = float(bids[0]["price"])
                elif asks:
                    current_price = float(asks[0]["price"]) * 0.9
                else:
                    continue  # mercato morto, skip

                # v7.4: shares = quanto abbiamo comprato (size/fill_price)
                # Per trade pre-v7.4 il price potrebbe essere il quoted, non il fill
                shares = trade.size / trade.price if trade.price > 0 else 0
                if shares <= 0:
                    continue

                # PnL = (prezzo corrente - prezzo acquisto) * shares
                pnl = shares * (current_price - trade.price)
                total_pnl += pnl
                if pnl >= 0:
                    n_profit += 1
                else:
                    n_loss += 1
                checked += 1

                # Rate limit: max 20 per ciclo (max_open_positions)
                if checked >= 20:
                    break

            except Exception:
                continue

        return total_pnl, n_profit, n_loss

    # Deposito totale reale su Polymarket (verificato manualmente)
    TOTAL_DEPOSITED = 6203.22

    def _fetch_real_portfolio(self) -> dict | None:
        """v10.8.4: Legge portfolio reale dalla Data API + saldo USDC.e on-chain.
        PnL calcolato come: (cash + posizioni) - deposito totale."""
        try:
            funder = self.config.creds.funder_address
            if not funder:
                return None
            resp = requests.get(
                f"https://data-api.polymarket.com/positions",
                params={"user": funder},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            positions = resp.json()
            if not isinstance(positions, list):
                return None

            total_current = 0.0
            n_redeemable = 0
            redeemable_val = 0.0
            n_active = 0

            for p in positions:
                cur_v = float(p.get("currentValue", 0))
                total_current += cur_v
                if p.get("redeemable"):
                    n_redeemable += 1
                    redeemable_val += cur_v
                elif cur_v > 0.01:
                    n_active += 1

            # Saldo USDC.e on-chain del proxy
            usdc_cash = 0.0
            try:
                usdc_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                call_data = "0x70a08231" + funder[2:].lower().zfill(64)
                for rpc in [
                    "https://polygon-bor-rpc.publicnode.com",
                    "https://rpc.ankr.com/polygon",
                ]:
                    try:
                        r = requests.post(rpc, json={
                            "jsonrpc": "2.0", "method": "eth_call",
                            "params": [{"to": usdc_e, "data": call_data}, "latest"],
                            "id": 1
                        }, timeout=8)
                        result = r.json().get("result", "0x0")
                        usdc_cash = int(result, 16) / 1e6
                        break
                    except Exception:
                        continue
            except Exception:
                pass

            portfolio_value = usdc_cash + total_current
            real_pnl = portfolio_value - self.TOTAL_DEPOSITED
            real_pnl_pct = (real_pnl / self.TOTAL_DEPOSITED * 100) if self.TOTAL_DEPOSITED > 0 else 0

            return {
                "deposited": self.TOTAL_DEPOSITED,
                "usdc_cash": round(usdc_cash, 2),
                "positions_value": round(total_current, 2),
                "portfolio_value": round(portfolio_value, 2),
                "real_pnl": round(real_pnl, 2),
                "real_pnl_pct": round(real_pnl_pct, 1),
                "n_positions": len(positions),
                "n_active": n_active,
                "n_redeemable": n_redeemable,
                "redeemable_value": round(redeemable_val, 2),
            }
        except Exception as e:
            logger.warning(f"[PORTFOLIO] Errore fetch: {e}")
            return None

    def _auto_scale_crypto(self):
        """
        v12.10.8: Auto-scale btc_latency e mro_kelly sizing basandosi sul WR.

        Regole:
        - Ogni 200 cicli (~100 min) controlla stats
        - Se WR >= 85% e >= 15 trade → raddoppia sizing (cap $200)
        - Se WR < 60% e >= 10 trade → dimezza sizing (floor $10)
        - Notifica via Telegram ogni scale
        - Max scaling: 4x dall'iniziale ($30→$60→$120→$200 cap)
        """
        MAX_SIZE_CAP = 200.0
        MIN_SIZE_FLOOR = 10.0
        SCALE_UP_WR = 0.85
        SCALE_UP_MIN_TRADES = 15
        SCALE_DOWN_WR = 0.60
        SCALE_DOWN_MIN_TRADES = 10

        for name, strat, size_attr, max_attr in [
            ("btc_latency", self.btc_latency, "base_size", "max_size"),
            ("mro_kelly", self.mro_kelly, "min_bet", "max_bet"),
        ]:
            stats = getattr(strat, 'stats', None)
            if stats is None:
                stats = getattr(strat, '_pnl_tracker', {})
                if isinstance(stats, dict):
                    trades_list = list(stats.values())
                    total = len(trades_list)
                    wins = sum(1 for t in trades_list if t.get('won'))
                else:
                    continue
            elif isinstance(stats, dict):
                total = stats.get('trades', 0)
                wins = stats.get('wins', 0)
            elif hasattr(stats, '__get__'):
                # property
                s = stats
                total = s.get('trades', 0)
                wins = s.get('wins', 0)
            else:
                continue

            if total == 0:
                continue

            wr = wins / total
            current_size = getattr(strat, size_attr, 0)
            current_max = getattr(strat, max_attr, 0)

            # Scale UP
            if wr >= SCALE_UP_WR and total >= SCALE_UP_MIN_TRADES:
                new_size = min(current_size * 2, MAX_SIZE_CAP)
                new_max = min(current_max * 2, MAX_SIZE_CAP)
                if new_size > current_size:
                    setattr(strat, size_attr, new_size)
                    setattr(strat, max_attr, new_max)
                    logger.info(
                        f"[AUTO-SCALE] {name}: SCALE UP! WR={wr:.0%} ({wins}/{total}) "
                        f"| {size_attr} ${current_size:.0f}→${new_size:.0f} "
                        f"| {max_attr} ${current_max:.0f}→${new_max:.0f}"
                    )
                    import asyncio
                    asyncio.ensure_future(self.telegram.send(
                        f"📈 <b>AUTO-SCALE UP</b>\n\n"
                        f"Strategy: {name}\n"
                        f"WR: {wr:.0%} ({wins}/{total} trades)\n"
                        f"Size: ${current_size:.0f} → ${new_size:.0f}\n"
                        f"Max: ${current_max:.0f} → ${new_max:.0f}"
                    ))

            # Scale DOWN
            elif wr < SCALE_DOWN_WR and total >= SCALE_DOWN_MIN_TRADES:
                new_size = max(current_size / 2, MIN_SIZE_FLOOR)
                new_max = max(current_max / 2, MIN_SIZE_FLOOR * 2)
                if new_size < current_size:
                    setattr(strat, size_attr, new_size)
                    setattr(strat, max_attr, new_max)
                    logger.info(
                        f"[AUTO-SCALE] {name}: SCALE DOWN! WR={wr:.0%} ({wins}/{total}) "
                        f"| {size_attr} ${current_size:.0f}→${new_size:.0f}"
                    )
                    import asyncio
                    asyncio.ensure_future(self.telegram.send(
                        f"📉 <b>AUTO-SCALE DOWN</b>\n\n"
                        f"Strategy: {name}\n"
                        f"WR: {wr:.0%} ({wins}/{total} trades)\n"
                        f"Size: ${current_size:.0f} → ${new_size:.0f}"
                    ))

    def _log_pnl_report(self):
        """v10.4: Report P&L periodico per strategia."""
        s = self.risk.status

        # Per-strategy breakdown
        lines = ["[PNL-REPORT] ═══ Snapshot ═══"]
        lines.append(f"  Capitale: ${s['capital']:.2f} | PnL oggi: ${s['daily_pnl']:+.2f} | W/L: {s['wins']}/{s['losses']} ({s['win_rate']:.0f}%)")
        lines.append(f"  Posizioni: {s['open']} aperte | Esposto: ${s['exposed']:.2f} | USDC: ${getattr(self, '_usdc_balance', 0):.2f}")

        # Unrealized
        u = getattr(self, '_unrealized_pnl', None)
        if u is not None:
            lines.append(f"  Unrealized: ${u:+.2f} ({getattr(self, '_unrealized_up', 0)} ▲ / {getattr(self, '_unrealized_down', 0)} ▼)")

        # v10.8.3: Portfolio reale
        rp = getattr(self, '_real_portfolio', None)
        if rp:
            lines.append(
                f"  REALE: dep=${rp['deposited']:.2f} tot=${rp['portfolio_value']:.2f} "
                f"PnL=${rp['real_pnl']:+.2f} ({rp['real_pnl_pct']:+.1f}%) "
                f"| cash=${rp['usdc_cash']:.2f} pos=${rp['positions_value']:.2f} "
                f"| {rp['n_active']} attive, {rp['n_redeemable']} redeemable"
            )

        # Per-strategy P&L
        if s['strategy_pnl']:
            parts = [f"{k}=${v:+.2f}" for k, v in sorted(s['strategy_pnl'].items())]
            lines.append(f"  Strategia: {' | '.join(parts)}")

        # Position breakdown per strategy
        by_strat = {}
        for t in self.risk.open_trades:
            by_strat.setdefault(t.strategy, []).append(t)
        for strat, trades in sorted(by_strat.items()):
            total_size = sum(t.size for t in trades)
            avg_age = sum((time.time() - t.timestamp) / 3600 for t in trades) / len(trades)
            lines.append(f"    {strat}: {len(trades)} pos, ${total_size:.0f} esposti, età media {avg_age:.0f}h")

        # Attribution report (se disponibile)
        if hasattr(self, 'attribution') and self.attribution:
            report = self.attribution.report
            if report.get('total_tracked', 0) > 0:
                lines.append(f"  Attribution: {report['total_tracked']} trade tracciati, {report.get('active_trades', 0)} attivi")

        lines.append("[PNL-REPORT] ═══════════════")
        logger.info("\n".join(lines))

        # v12.10: Update dashboard + Telegram alerts
        if hasattr(self, 'dashboard'):
            self.dashboard.update_capital(s['capital'], s['open'])
        if hasattr(self, 'telegram'):
            # Drawdown alert
            daily_pnl = s.get('daily_pnl', 0)
            capital = s.get('capital', 1)
            dd_pct = abs(daily_pnl / capital * 100) if daily_pnl < 0 and capital > 0 else 0
            if dd_pct >= 10:
                action = "HALT" if dd_pct >= 20 else "Monitoring"
                asyncio.ensure_future(self.telegram.send(
                    f"⚠️ <b>DRAWDOWN ALERT</b>\n\n"
                    f"📉 Daily drawdown: -{dd_pct:.1f}%\n"
                    f"💰 Daily PnL: ${daily_pnl:+.2f}\n"
                    f"🔧 Action: {action}"
                ))

    async def _log_position_health(self):
        """v10.4: Health check posizioni aperte."""
        trades = self.risk.open_trades
        if not trades:
            return

        now = time.time()
        lines = ["[HEALTH] ═══ Position Health Check ═══"]

        # Aggregati
        total_size = sum(t.size for t in trades)
        ages = [(now - t.timestamp) / 3600 for t in trades]
        avg_age = sum(ages) / len(ages)
        stale_24h = sum(1 for a in ages if a > 24)
        stale_48h = sum(1 for a in ages if a > 48)

        lines.append(f"  {len(trades)} posizioni | ${total_size:.0f} esposti | età media {avg_age:.1f}h")
        if stale_24h:
            lines.append(f"  ⚠ {stale_24h} posizioni >24h ({stale_48h} >48h)")

        # Check prezzi correnti (max 10 per non saturare API)
        n_profit = 0
        n_loss = 0
        n_checked = 0
        worst = None
        worst_pnl_pct = 0.0

        for trade in trades[:10]:
            try:
                book = await asyncio.to_thread(self.api.get_order_book, trade.token_id)
                bids = book.get("bids", [])
                if not bids:
                    continue
                bid = float(bids[0]["price"])
                if trade.price > 0 and bid > 0:
                    pnl_pct = (bid - trade.price) / trade.price
                    if pnl_pct >= 0:
                        n_profit += 1
                    else:
                        n_loss += 1
                    if pnl_pct < worst_pnl_pct:
                        worst_pnl_pct = pnl_pct
                        worst = trade
                    n_checked += 1
            except Exception:
                continue

        if n_checked:
            lines.append(f"  Prezzi controllati: {n_checked} — {n_profit} ▲ profitto, {n_loss} ▼ perdita")
        if worst:
            age_h = (now - worst.timestamp) / 3600
            lines.append(
                f"  Peggiore: {worst.strategy} '{worst.reason[:40]}' "
                f"PnL={worst_pnl_pct:+.1%} età={age_h:.0f}h"
            )

        # Per-strategy concentration
        by_strat = {}
        for t in trades:
            by_strat.setdefault(t.strategy, {"count": 0, "size": 0.0})
            by_strat[t.strategy]["count"] += 1
            by_strat[t.strategy]["size"] += t.size

        for strat, info in sorted(by_strat.items(), key=lambda x: -x[1]["size"]):
            pct = (info["size"] / total_size * 100) if total_size else 0
            lines.append(f"    {strat}: {info['count']} pos, ${info['size']:.0f} ({pct:.0f}%)")

        lines.append("[HEALTH] ══════════════════════════")
        logger.info("\n".join(lines))

    def _reload_auto_optimized_params(self):
        """v12.5.2: Reload auto-optimized params for ALL strategies."""
        import json as _json
        from pathlib import Path as _Path

        # Strategy → (object, param→attr mapping)
        strategy_map = {
            "weather": (self.weather, {
                "min_edge": "min_edge",
                "min_confidence": "min_confidence",
                "min_payoff": "min_payoff",
                "max_price": "max_price",
                "min_sources_high_price": "min_sources_high_price",
                "high_price_threshold": "high_price_threshold",
                "min_edge_same_day": "min_edge_same_day",
                "min_edge_1d": "min_edge_1d",
                "min_edge_2d": "min_edge_2d",
                "ev_minimum": "ev_minimum",
                "meta_label_threshold": "meta_label_threshold",
                "city_tier2_max_bet": "city_tier2_max_bet",
                "max_weather_bet": "max_weather_bet",
            }),
            "favorite_longshot": (self.favorite_longshot, {
                "min_price": "MIN_PRICE",
                "max_price": "MAX_PRICE",
                "base_alpha": "BASE_ALPHA",
                "min_edge": "MIN_EDGE",
                "min_volume": "MIN_VOLUME",
                "max_bet": "MAX_BET",
            }),
            "abandoned_position": (self.abandoned_position, {
                "min_near_certain_price": "MIN_NEAR_CERTAIN_PRICE",
                "max_near_certain_price": "MAX_NEAR_CERTAIN_PRICE",
                "max_volume_24h": "MAX_VOLUME_24H",
                "max_hours_to_resolution": "MAX_HOURS_TO_RESOLUTION",
                "min_hours_to_resolution": "MIN_HOURS_TO_RESOLUTION",
                "max_position": "MAX_POSITION",
            }),
            "negrisk_arb": (self.negrisk_arb, {
                "min_deviation": "MIN_DEVIATION",
                "max_arb_size": "MAX_ARB_SIZE",
                "min_liquidity": "MIN_LIQUIDITY",
                "cooldown_minutes": "COOLDOWN_MINUTES",
            }),
            "holding_rewards": (self.holding_rewards, {
                "max_bet_per_market": "MAX_BET_PER_MARKET",
                "min_holding_days": "MIN_HOLDING_DAYS",
                "max_positions": "MAX_POSITIONS",
            }),
            "econ_sniper": (self.econ_sniper, {
                "nfp_surprise_threshold": "NFP_SURPRISE_THRESHOLD",
                "unemployment_surprise_threshold": "UNEMPLOYMENT_SURPRISE_THRESHOLD",
                "cpi_surprise_threshold": "CPI_SURPRISE_THRESHOLD",
                "max_bet": "MAX_BET",
            }),
            "market_making": (self.mm, {
                "min_spread": "MIN_SPREAD",
                "max_spread": "MAX_SPREAD",
                "order_size": "ORDER_SIZE",
                "max_inventory_per_side": "MAX_INVENTORY_PER_SIDE",
                "max_concurrent_markets": "MAX_CONCURRENT_MARKETS",
            }),
            # v12.10: crowd_sport e crowd_prediction disabilitati — skip se non inizializzati
            **({"crowd_sport": (self.crowd_sport, {
                "min_edge": "MIN_EDGE",
                "max_bet": "MAX_BET",
                "kelly_fraction": "KELLY_FRACTION",
                "min_volume": "MIN_VOLUME",
                "max_markets_per_scan": "MAX_MARKETS_PER_SCAN",
            })} if hasattr(self, 'crowd_sport') else {}),
            **({"crowd_prediction": (self.crowd_prediction, {
                "min_edge": "MIN_EDGE",
                "max_bet": "MAX_BET",
                "kelly_fraction": "KELLY_FRACTION",
                "min_volume": "MIN_VOLUME",
                "max_markets_per_scan": "MAX_MARKETS_PER_SCAN",
            })} if hasattr(self, 'crowd_prediction') else {}),
        }

        for strat_name, (strat_obj, param_map) in strategy_map.items():
            param_file = _Path(__file__).parent / "logs" / f"auto_optimizer_applied_{strat_name}.json"
            if not param_file.exists():
                continue
            try:
                history = _json.loads(param_file.read_text())
                if not isinstance(history, list) or not history:
                    continue
                latest = history[-1]
                # Skip if score is negative (optimizer found nothing good)
                if latest.get("score", 0) < 0:
                    continue
                params = latest.get("params", {})
                changed = []
                for opt_key, attr_name in param_map.items():
                    if opt_key not in params:
                        continue
                    current = getattr(strat_obj, attr_name, None)
                    new_val = params[opt_key]
                    if current is not None and current != new_val:
                        setattr(strat_obj, attr_name, new_val)
                        changed.append(f"{attr_name}: {current} → {new_val}")
                if changed:
                    logger.info(
                        f"[AUTO-OPT] {strat_name}: reloaded {len(changed)} params — "
                        + ", ".join(changed[:5])
                    )
            except Exception as e:
                logger.debug(f"[AUTO-OPT] {strat_name} reload error: {e}")

    def _darwinian_reweight(self):
        """
        v12.2: Darwinian Weighting (ispirato da atlas-gic).
        Ribilancia budget tra strategie basato su performance recente.
        Top performer → budget *1.05, bottom → *0.95.
        Pesi clamped tra 0.5x e 2.0x del budget base.
        """
        import json as _json
        from pathlib import Path as _Path

        ACTIVE_STRATEGIES = ["weather", "resolution_sniper"]
        WEIGHT_FILE = _Path(__file__).parent / "logs" / "darwinian_weights.json"

        # Carica pesi precedenti o inizializza a 1.0
        weights = {}
        if WEIGHT_FILE.exists():
            try:
                weights = _json.loads(WEIGHT_FILE.read_text())
            except Exception:
                pass
        for s in ACTIVE_STRATEGIES:
            if s not in weights:
                weights[s] = 1.0

        # Calcola WR recente per strategia (ultimi 50 trade chiusi)
        perf = {}
        for s in ACTIVE_STRATEGIES:
            strat_trades = [
                t for t in self.risk.trades
                if t.strategy == s and t.result in ("WIN", "LOSS")
            ][-50:]
            if len(strat_trades) >= 5:
                wins = sum(1 for t in strat_trades if t.result == "WIN")
                perf[s] = wins / len(strat_trades)

        if len(perf) < 2:
            return  # serve almeno 2 strategie con dati

        # Ranking: top quartile *1.05, bottom quartile *0.95
        sorted_strats = sorted(perf.keys(), key=lambda s: perf[s], reverse=True)
        top = sorted_strats[:max(1, len(sorted_strats) // 4 + 1)]
        bottom = sorted_strats[-max(1, len(sorted_strats) // 4 + 1):]

        changes = []
        for s in ACTIVE_STRATEGIES:
            old_w = weights[s]
            if s in top:
                weights[s] = min(2.0, old_w * 1.05)
            elif s in bottom:
                weights[s] = max(0.5, old_w * 0.95)
            if abs(weights[s] - old_w) > 0.001:
                changes.append(f"{s}: {old_w:.2f}→{weights[s]:.2f} (WR={perf.get(s,0):.0%})")

        # Applica pesi ai budget
        for s in ACTIVE_STRATEGIES:
            base_budget = self.config.capital_for(s)
            new_budget = base_budget * weights[s]
            self.risk.set_strategy_budget(s, new_budget)

        self._darwinian_weights = weights

        # Salva
        try:
            with open(WEIGHT_FILE, "w") as f:
                _json.dump(weights, f, indent=2)
        except Exception:
            pass

        if changes:
            logger.info(
                f"[DARWINIAN] Reweight: {', '.join(changes)}"
            )

    def _auto_compound(self):
        """
        v12.3: Auto-Compound — ricalcola capitale reale e scala budget/bet proporzionalmente.
        Legge USDC balance + valore posizioni aperte, aggiorna total_capital e tutti i budget.
        """
        import json as _json
        from pathlib import Path as _Path

        # Capitale reale = USDC disponibile + valore mark-to-market posizioni
        usdc = self._usdc_balance
        if usdc <= 0:
            return

        # Stima valore posizioni aperte (approssimazione: size * current_price)
        positions_value = 0.0
        for t in self.risk.open_trades:
            positions_value += t.size  # Conservativo: usa invested amount

        real_capital = usdc + positions_value

        # Floor: mai scendere sotto reserve_floor (20%)
        min_capital = self.config.risk.total_capital * 0.5  # mai sotto 50% del capitale iniziale
        real_capital = max(real_capital, min_capital)

        old_capital = self.config.risk.total_capital
        if abs(real_capital - old_capital) < 50:
            return  # Variazione minima, skip

        # Aggiorna capitale nel config e risk manager
        self.config.risk.total_capital = real_capital
        self.risk.capital = real_capital
        self.risk.config.total_capital = real_capital

        # Riscala budget per strategia
        for strat in ["weather", "resolution_sniper", "arb_gabagool", "arbitrage",
                       "data_driven", "event_driven", "high_prob_bond", "whale_copy"]:
            base = self.config.capital_for(strat)
            # Applica peso darwiniano se presente
            dw = self._darwinian_weights.get(strat, 1.0)
            self.risk.set_strategy_budget(strat, base * dw)

        # Scala max bet proporzionalmente (ratio vs baseline $7K)
        ratio = real_capital / 7000.0
        import weather as _weather_mod
        _weather_mod.MAX_WEATHER_BET = round(min(200, max(30, 80 * ratio)), 0)
        _weather_mod.CITY_TIER2_MAX_BET = round(min(80, max(15, 35 * ratio)), 0)

        try:
            from strategies import favorite_longshot as _fl_mod
            _fl_mod.MAX_BET = round(min(120, max(20, 50 * ratio)), 0)
        except Exception:
            pass

        # Scala market making max inventory
        try:
            from strategies import market_making as _mm_mod
            _mm_mod.MAX_INVENTORY_PER_SIDE = round(min(500, max(50, 200 * ratio)), 0)
            _mm_mod.ORDER_SIZE = round(min(60, max(10, 25 * ratio)), 0)
        except Exception:
            pass

        # Log
        logger.info(
            f"[AUTO-COMPOUND] Capitale: ${old_capital:.0f} → ${real_capital:.0f} "
            f"(USDC=${usdc:.0f} + pos=${positions_value:.0f}) "
            f"ratio={ratio:.2f}x | weather_max=${_weather_mod.MAX_WEATHER_BET}"
        )

        # Salva snapshot
        compound_file = _Path(__file__).parent / "logs" / "auto_compound.json"
        try:
            import time as _t
            entry = {
                "timestamp": _t.time(),
                "old_capital": old_capital,
                "new_capital": real_capital,
                "usdc": usdc,
                "positions_value": positions_value,
                "ratio": ratio,
            }
            history = []
            if compound_file.exists():
                history = _json.loads(compound_file.read_text())
            history.append(entry)
            history = history[-100:]  # keep last 100
            with open(compound_file, "w") as f:
                _json.dump(history, f, indent=2)
        except Exception:
            pass

    def _check_auto_revert(self):
        """
        v12.2: Auto-Revert (ispirato da atlas-gic keep/revert).
        Se l'AutoOptimizer ha applicato parametri e dopo 72h il PnL
        è peggiorato, reverta ai parametri precedenti.
        """
        import json as _json
        from pathlib import Path as _Path

        applied_file = _Path(__file__).parent / "logs" / "auto_optimizer_applied_weather.json"
        if not applied_file.exists():
            return

        try:
            history = _json.loads(applied_file.read_text())
            if not isinstance(history, list) or len(history) < 2:
                return  # serve almeno 1 apply per valutare

            latest = history[-1]
            applied_ts = latest.get("timestamp", "")
            if not applied_ts:
                return

            # Parse timestamp
            from datetime import datetime as _dt
            applied_dt = _dt.strptime(applied_ts, "%Y-%m-%d %H:%M:%S")
            hours_since = (
                _dt.now() - applied_dt
            ).total_seconds() / 3600

            if hours_since < 72:
                return  # troppo presto per giudicare

            # Già valutato? Check flag
            if latest.get("_revert_checked"):
                return

            # Calcola WR post-apply (trade dopo l'apply timestamp)
            applied_epoch = applied_dt.timestamp()
            post_trades = [
                t for t in self.risk.trades
                if t.strategy == "weather"
                and t.result in ("WIN", "LOSS")
                and t.timestamp > applied_epoch
            ]

            if len(post_trades) < 10:
                return  # non abbastanza dati

            post_wins = sum(1 for t in post_trades if t.result == "WIN")
            post_wr = post_wins / len(post_trades)

            # WR pre-apply (dalla metrica salvata nell'apply)
            pre_wr = latest.get("metrics", {}).get("wr", 65) / 100

            # Se WR post è >15 punti peggiore → revert
            if post_wr < pre_wr - 0.15:
                # Revert ai parametri precedenti
                if len(history) >= 2:
                    prev_params = history[-2].get("params", {})
                else:
                    prev_params = {}

                if prev_params:
                    # Scrivi revert come nuovo entry
                    revert_entry = {
                        "strategy": "weather",
                        "timestamp": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "improvement_pct": 0,
                        "closed_trades": len(post_trades),
                        "score": 0,
                        "metrics": latest.get("metrics", {}),
                        "params": prev_params,
                        "changes": [],
                        "_reverted_from": applied_ts,
                        "_reason": f"post_wr={post_wr:.1%} vs pre_wr={pre_wr:.1%} "
                                   f"({len(post_trades)} trades in {hours_since:.0f}h)",
                    }
                    history.append(revert_entry)
                    logger.warning(
                        f"[AUTO-REVERT] Weather params reverted! "
                        f"Post-apply WR={post_wr:.1%} vs expected={pre_wr:.1%} "
                        f"({len(post_trades)} trades in {hours_since:.0f}h)"
                    )
                else:
                    logger.info(
                        f"[AUTO-REVERT] Would revert but no previous params. "
                        f"Post WR={post_wr:.1%} vs {pre_wr:.1%}"
                    )
            else:
                logger.info(
                    f"[AUTO-REVERT] Keep! Post-apply WR={post_wr:.1%} "
                    f"vs expected={pre_wr:.1%} ({len(post_trades)} trades, "
                    f"{hours_since:.0f}h) — params validated"
                )

            # Segna come valutato
            latest["_revert_checked"] = True
            with open(applied_file, "w") as f:
                _json.dump(history, f, indent=2)

        except Exception as e:
            logger.debug(f"[AUTO-REVERT] Error: {e}")

    async def _weather_fetch_loop(self):
        """v10.8: Fetch asincrono dei weather markets extra (offset 400-1400).
        Gira ogni 60s in background, popola _weather_extra_cache senza bloccare il main loop."""
        # v13.3: SOSPESO — weather disabilitato, questo loop blocca l'event loop
        return
        await asyncio.sleep(2)  # attendi primo fetch REST
        while self._running:
            try:
                extra = []
                base_ids = {m.id for m in self._shared_markets_cache} if self._shared_markets_cache else set()
                for _off in range(400, 1400, 200):
                    page = await asyncio.to_thread(self.api.fetch_markets, limit=200, offset=_off)
                    if not page:
                        break
                    new = [m for m in page if m.id not in base_ids]
                    extra.extend(new)
                    base_ids.update(m.id for m in new)
                self._weather_extra_cache = extra
                self._weather_extra_last = time.time()
                if extra:
                    logger.debug(f"[WEATHER-ASYNC] Cache aggiornata: {len(extra)} mercati extra")
            except Exception as e:
                logger.debug(f"[WEATHER-ASYNC] Errore fetch: {e}")
            await asyncio.sleep(60)

    async def _model_update_loop(self):
        """v10.8.5: Latency Hunter — monitora orari di rilascio modelli meteo.

        GFS rilascia nuovi dati ~3.5h dopo il model run:
          run 00Z → disponibile ~03:30 UTC
          run 06Z → disponibile ~09:30 UTC
          run 12Z → disponibile ~15:30 UTC
          run 18Z → disponibile ~21:30 UTC

        ECMWF rilascia ~6h dopo:
          run 00Z → disponibile ~06:00 UTC
          run 12Z → disponibile ~18:00 UTC

        Quando un nuovo model run e' disponibile:
        1. Invalida cache forecast
        2. Forza re-fetch di tutte le citta'
        3. Confronta nuovo vs vecchio forecast
        4. Se shift > 1°C → priority scan (il mercato e' ancora sul vecchio prezzo)
        """
        from datetime import timezone
        await asyncio.sleep(10)  # attendi init

        # Orari di disponibilita' dati (UTC hours)
        # GFS: ogni 6h con ~3.5h delay, ECMWF: ogni 12h con ~6h delay
        MODEL_UPDATE_HOURS = [3.5, 6.0, 9.5, 15.5, 18.0, 21.5]
        CHECK_INTERVAL = 120  # controlla ogni 2 min
        last_triggered_hour = -1.0

        logger.info(
            "[LATENCY-HUNTER] Attivo — monitora model updates "
            f"({len(MODEL_UPDATE_HOURS)} finestre/giorno: "
            f"{', '.join(f'{h:.1f}Z' for h in MODEL_UPDATE_HOURS)})"
        )

        while self._running:
            try:
                now_utc = datetime.now(timezone.utc)
                current_hour = now_utc.hour + now_utc.minute / 60.0

                # Trova la finestra di update piu' vicina passata da poco
                for update_hour in MODEL_UPDATE_HOURS:
                    # Finestra: da update_hour a +30 min dopo
                    time_since = current_hour - update_hour
                    if time_since < 0:
                        time_since += 24  # wrap around

                    # Trigger se siamo entro 30 min dal drop E non gia' triggerato
                    if 0 <= time_since <= 0.5 and abs(last_triggered_hour - update_hour) > 0.5:
                        last_triggered_hour = update_hour
                        model_name = "ECMWF" if update_hour in [6.0, 18.0] else "GFS"
                        logger.info(
                            f"[LATENCY-HUNTER] {model_name} model update detected "
                            f"(drop window {update_hour:.1f}Z, now={current_hour:.1f}Z)"
                        )

                        # 1. Invalida cache
                        self.weather_feed.invalidate_cache(reason=f"{model_name} update {update_hour:.0f}Z")

                        # 2. Re-fetch tutte le citta' (in background thread)
                        cities = list(WEATHER_CITIES.keys()) if 'WEATHER_CITIES' in dir() else []
                        if not cities:
                            from utils.weather_feed import WEATHER_CITIES as WC
                            cities = list(WC.keys())

                        shift_detected = False
                        for city in cities:
                            try:
                                await asyncio.to_thread(self.weather_feed.get_forecast, city)
                                # Controlla shift per oggi e domani
                                today = now_utc.strftime("%Y-%m-%d")
                                from datetime import timedelta
                                tomorrow = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
                                for date in [today, tomorrow]:
                                    shift = self.weather_feed.get_forecast_shift(city, date)
                                    if shift is not None and abs(shift) >= 1.0:
                                        shift_detected = True
                                        logger.info(
                                            f"[LATENCY-HUNTER] SHIFT RILEVATO {city} {date}: "
                                            f"{shift:+.1f}°C — mercato probabilmente non aggiornato"
                                        )
                            except Exception as e:
                                logger.debug(f"[LATENCY-HUNTER] Errore fetch {city}: {e}")

                        # 3. Se shift significativo, forza priority scan
                        if shift_detected:
                            self._weather_priority_scan = True
                            logger.info(
                                "[LATENCY-HUNTER] Priority scan attivato — "
                                "forecast shift >= 1°C rilevato"
                            )
                        else:
                            logger.info(
                                "[LATENCY-HUNTER] Nessun shift significativo — "
                                "mercato gia' allineato"
                            )
                        break  # solo una finestra per iterazione

            except Exception as e:
                logger.debug(f"[LATENCY-HUNTER] Errore loop: {e}")

            await asyncio.sleep(CHECK_INTERVAL)

    async def _dashboard_loop(self):
        while self._running:
            await asyncio.sleep(20)
            if self.binance.price > 0:
                dashboard(
                    self.risk, self.config.paper_trading, self._cycle,
                    unrealized_pnl=getattr(self, '_unrealized_pnl', None),
                    usdc_balance=getattr(self, '_usdc_balance', None),
                    real_portfolio=getattr(self, '_real_portfolio', None),
                )
                # v10.8: Telegram P&L report (ogni ora, rate-limited dal notifier)
                s = self.risk.status
                # v12.10.8: calcola PnL settimanale e totale da trades.json
                weekly_pnl = 0.0
                alltime_pnl = 0.0
                try:
                    import json as _json
                    from pathlib import Path as _Path
                    import datetime as _dt
                    trades_file = _Path(__file__).parent / "logs" / "trades.json"
                    if trades_file.exists():
                        trades_data = _json.loads(trades_file.read_text())
                        week_ago = (_dt.datetime.now() - _dt.timedelta(days=7)).isoformat()
                        for t in trades_data:
                            pnl = t.get("pnl") or 0
                            alltime_pnl += pnl
                            closed = t.get("closed_at", "") or ""
                            if closed >= week_ago:
                                weekly_pnl += pnl
                    # Aggiungi PnL sessione corrente (non ancora in trades.json)
                    alltime_pnl += s.get('daily_pnl', 0)
                    weekly_pnl += s.get('daily_pnl', 0)
                except Exception:
                    pass
                await self.telegram.notify_pnl_report(
                    capital=s['capital'], daily_pnl=s['daily_pnl'],
                    total_trades=s['total_trades'], win_rate=s['win_rate'],
                    open_positions=s['open'],
                    usdc_balance=getattr(self, '_usdc_balance', 0),
                    unrealized_pnl=getattr(self, '_unrealized_pnl', 0) or 0,
                    strategy_pnl=s['strategy_pnl'],
                    real_portfolio=getattr(self, '_real_portfolio', None),
                    weekly_pnl=weekly_pnl,
                    alltime_pnl=alltime_pnl,
                )

    async def stop(self):
        logger.info("Arresto bot...")
        self._running = False
        if not self.config.paper_trading:
            self.api.cancel_all()
        await self.binance.stop()
        await self.ws_feed.stop()
        self.risk.save_trades()

        # v9.0: Cleanup storage
        if self.db:
            try:
                self.db.close()
            except Exception:
                pass
        if self.event_bus:
            try:
                self.event_bus.close()
            except Exception:
                pass

        s = self.risk.status
        logger.info(
            f"\n{'=' * 50}\n"
            f"  SESSIONE CONCLUSA\n"
            f"{'=' * 50}\n"
            f"  PnL: ${s['daily_pnl']:+.2f}\n"
            f"  Trades: {s['total_trades']} (W:{s['wins']} L:{s['losses']})\n"
            f"  Win rate: {s['win_rate']:.1f}%\n"
            f"  Capitale finale: ${s['capital']:.2f}\n"
            f"{'=' * 50}"
        )


# ── Entry Point ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Multi-Strategy Bot")
    parser.add_argument("--live", action="store_true", help="Trading reale")
    args = parser.parse_args()

    config = Config.from_env()

    # Validazione OBBLIGATORIA — blocca se l'allocazione non somma a 100
    errors = config.validate()
    if errors:
        print("\n*** ERRORE CONFIGURAZIONE ***")
        for e in errors:
            print(f"  - {e}")
        print("\nCorreggi il file .env e riprova.")
        sys.exit(1)

    if args.live:
        config.paper_trading = False
        print("\n*** ATTENZIONE: MODALITA' LIVE — SOLDI VERI ***")
        if input("Digita 'CONFERMO' per procedere: ") != "CONFERMO":
            print("Annullato.")
            sys.exit(0)

    setup_logging(config.log_level)
    bot = MultiStrategyBot(config)

    loop = asyncio.new_event_loop()

    def shutdown(sig, _):
        logger.info(f"Segnale {sig}, arresto...")
        loop.call_soon_threadsafe(lambda: loop.create_task(bot.stop()))

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
