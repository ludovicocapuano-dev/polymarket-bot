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
from utils.weather_feed import WeatherFeed
from utils.arbbets_feed import ArbBetsFeed
from utils.lunarcrush_feed import LunarCrushFeed
from utils.cryptoquant_feed import CryptoQuantFeed
from utils.finlight_feed import FinlightFeed
from utils.gdelt_feed import GDELTFeed
from utils.nansen_feed import NansenFeed
from utils.dome_feed import DomeFeed
from utils.polymarket_ws_feed import PolymarketWSFeed
from utils.vpin_monitor import VPINMonitor
from utils.risk_manager import RiskManager
from utils.redeemer import Redeemer

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
from utils.uma_monitor import UmaMonitor

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
              unrealized_pnl: float = None, usdc_balance: float = None):
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

    print(f"""
{'=' * 65}
  [{mode}] Ciclo #{cycle} | {datetime.now().strftime('%H:%M:%S')}
{'─' * 65}
  Capitale: ${s['capital']:>10,.2f}  |  PnL oggi: ${s['daily_pnl']:>+8,.2f}
  USDC:     {usdc_str}  |  Unrealized: {upnl_str}
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
        self.weather_feed = WeatherFeed()
        self.arbbets_feed = ArbBetsFeed()
        self.lunar_feed = LunarCrushFeed()
        self.cquant_feed = CryptoQuantFeed()
        self.finlight_feed = FinlightFeed()
        self.gdelt_feed = GDELTFeed()
        self.nansen_feed = NansenFeed()
        self.dome_feed = DomeFeed()
        self.ws_feed = PolymarketWSFeed()
        self.vpin_monitor = VPINMonitor()  # v9.2.1: VPIN toxic flow detection
        self.risk = RiskManager(config.risk)

        # Ricarica posizioni aperte dal disco (sopravvive ai restart)
        # v5.6: max_age=96h — serve per POSITION-MGR che vende posizioni >48h
        # (prima era 6h e il MGR non vedeva mai le posizioni vecchie!)
        self.risk.load_open_positions(max_age_hours=96.0)

        # v5.7: Rimossa la safety check distruttiva che cancellava TUTTE
        # le posizioni se superavano l'80% del limite. Causava perdita tracking.
        # Ora logga solo un warning.
        max_pos = config.risk.max_open_positions
        if len(self.risk.open_trades) > max_pos * 0.9:
            logger.warning(
                f"[STARTUP] {len(self.risk.open_trades)} posizioni caricate "
                f"(>{max_pos*0.9:.0f}) — vicino al limite {max_pos}"
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

        self.weather = WeatherStrategy(
            self.api, self.risk, self.weather_feed,
            min_edge=0.03,  # v5.3: 3% (same-day 1.8% via 0.6x) + Bayesian posterior
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

        # v7.0: market_making DISABILITATO (necessita $2K+ budget)
        # self.mm = MarketMakingStrategy(self.api, self.risk)
        # self.risk.set_strategy_budget("market_making", config.capital_for("market_making"))

        self.whale = WhaleCopyStrategy(
            self.api, self.risk,
            min_edge=0.03,
        )
        self.risk.set_strategy_budget("whale_copy", config.capital_for("whale_copy"))

        # v5.9.2: Resolution Sniper
        self.uma_monitor = UmaMonitor()
        self.sniper = ResolutionSniperStrategy(
            self.api, self.risk,
            uma_monitor=self.uma_monitor,
            min_edge=0.03,  # 3% — fee-free markets
        )
        self.risk.set_strategy_budget("resolution_sniper", config.risk.total_capital * 0.02)

        # ── Auto-Redeem vincite risolte ──
        priv_key = config.creds.private_key.strip()
        funder = config.creds.funder_address.strip() if config.creds.funder_address else ""
        self.redeemer = Redeemer(priv_key, funder) if funder else None
        if self.redeemer and self.redeemer.available:
            logger.info("Auto-redeem attivo (web3 connesso a Polygon)")
        elif self.redeemer:
            logger.warning("Auto-redeem disabilitato (web3 non disponibile)")

        self._running = False
        self._cycle = 0
        self._usdc_balance = 0.0
        self._resolve_last_check = 0.0
        self._resolved_cache: set[str] = set()
        self._shared_markets_cache: list = []

        # ── v9.0: Layer 2 — Signal Validator + Devil's Advocate ──
        self.devils_advocate = DevilsAdvocate(risk_manager=self.risk)
        self.signal_validator = SignalValidator(
            devil_advocate=self.devils_advocate,
            vpin_monitor=self.vpin_monitor,  # v9.2.1: gate #8 VPIN toxic flow
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
        self.risk.correlation_monitor = self.correlation_monitor
        self.risk.ws_feed = self.ws_feed  # v9.2.1: flash move protection
        self.risk.vpin_monitor = self.vpin_monitor  # v9.2.1: VPIN toxic flow
        self.ws_feed.on_trade = self.vpin_monitor.record_trade  # v9.2.1: feed VPIN
        self.tail_risk = TailRiskAgent(risk_manager=self.risk)

        # ── v9.0: Layer 1 — Orchestrator ──
        self.orchestrator = OrchestratorAgent()

        # ── v9.0: Layer 4 — Execution Engine ──
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

    async def start(self):
        print(BANNER)

        if not self.config.paper_trading:
            logger.info("Autenticazione Polymarket...")
            if not self.api.authenticate():
                logger.error("Autenticazione fallita!")
                sys.exit(1)

        paper = self.config.paper_trading
        logger.info(f"Bot avviato in modalita' {'PAPER' if paper else 'LIVE'}")
        logger.info(
            f"Allocazione v9.2.1: BOND={self.config.allocation.high_prob_bond}% "
            f"GAB={self.config.allocation.arb_gabagool}% "
            f"WEATHER={self.config.allocation.weather}% "
            f"ARB={self.config.allocation.arbitrage}% "
            f"DATA={self.config.allocation.data_driven}% "
            f"EVENT={self.config.allocation.event_driven}% "
            f"WHALE={self.config.allocation.whale_copy}%"
        )

        self._running = True

        await asyncio.gather(
            self.binance.connect(),
            self.uma_monitor.start(),  # v5.9.2: UMA resolution monitor
            self.ws_feed.connect(),    # v9.2: Polymarket WS price feed
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

                # ── v9.2: Fetch ibrido REST + WebSocket ──
                # REST full refresh ogni 20 cicli (~60s) o se WS non disponibile
                # WS aggiorna solo i prezzi tra un refresh e l'altro
                if self._cycle == 1 or self._cycle % 20 == 0 or not self.ws_feed.available:
                    shared_markets = self.api.fetch_markets(limit=400)
                    if not shared_markets:
                        logger.warning(f"Ciclo #{self._cycle}: 0 mercati dall'API, skip")
                        await asyncio.sleep(self.config.poll_interval)
                        continue
                    self._shared_markets_cache = shared_markets
                    self.ws_feed.register_markets(shared_markets)
                    ws_tag = " (REST)" if not self.ws_feed.available else " (REST+WS sync)"
                    logger.debug(
                        f"Ciclo #{self._cycle}: {len(shared_markets)} mercati fetchati{ws_tag}"
                    )
                else:
                    # Usa cache con prezzi aggiornati dal WS
                    shared_markets = self.ws_feed.update_prices(self._shared_markets_cache)
                    logger.debug(
                        f"Ciclo #{self._cycle}: {len(shared_markets)} mercati da cache WS"
                    )

                # ── v9.0: Orchestrator — prioritizzazione mercati ──
                try:
                    orch_tasks = await self.orchestrator.prioritize(shared_markets)
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

                # Skip trading se saldo troppo basso (ma auto-close continua!)
                _can_trade = getattr(self, '_usdc_balance', 999) >= 2.0

                # ── NOTA: ogni strategia ha il suo try/except isolato ──
                # Se una strategia crasha, le altre continuano a funzionare.
                # Prima di v4.0.1, un errore in weather bloccava arb+data+event.

                # ── 0. GABAGOOL Arbitraggio Puro (priorita' MASSIMA — profitto garantito) ──
                # v5.9.1: ogni 10 cicli, scansiona 1000 mercati per arb a largo raggio
                try:
                    if self._cycle % 10 == 0:
                        extended_markets = self.api.fetch_markets(limit=1000)
                        gab_opps = await self.gabagool.scan(shared_markets=extended_markets or shared_markets)
                    else:
                        gab_opps = await self.gabagool.scan(shared_markets=shared_markets)
                    if _can_trade:
                        for opp in gab_opps[:5]:  # Piu' trade: l'arb e' risk-free
                            if not self._running:
                                break
                            await self.gabagool.execute(opp, paper=paper)
                except Exception as e:
                    logger.error(f"[GABAGOOL] Errore strategia: {e}", exc_info=True)

                # ── 0.5. Resolution Sniper + High-Prob Bonds ──
                # v5.9.2: profitto quasi-certo da resolution sniping e bonding
                try:
                    sniper_signals = await self.sniper.scan(shared_markets=shared_markets)
                    if _can_trade:
                        for sig in sniper_signals[:5]:  # Max 5 (quasi risk-free)
                            if not self._running:
                                break
                            await self.sniper.execute(sig, paper=paper)
                except Exception as e:
                    logger.error(f"[SNIPER] Errore strategia: {e}", exc_info=True)

                # ── 1. Crypto 5-Min — DISABILITATO v7.0 (fees > edge, Kelly negativo) ──

                # ── 2. Weather (previsioni meteo — mercati giornalieri) ──
                try:
                    weather_opps = await self.weather.scan(shared_markets=shared_markets)
                    if _can_trade:
                        for opp in weather_opps[:3]:
                            if not self._running:
                                break
                            signal = from_weather_opportunity(opp)
                            kelly = self.risk.kelly_size(opp.forecast_prob, signal.price, "weather")
                            report = self.signal_validator.validate(signal, trade_size=kelly)
                            if report.result == ValidationResult.TRADE:
                                self.attribution.record_entry(
                                    opp.market.tokens.get(opp.side.lower(), ""),
                                    "weather", "weather", "weather",
                                    edge_predicted=opp.edge, validation_score=report.score,
                                )
                                await self.weather.execute(opp, paper=paper)
                except Exception as e:
                    logger.error(f"[WEATHER] Errore strategia: {e}", exc_info=True)

                # ── 3. Arbitraggio ──
                try:
                    arb_opps = await self.arb.scan(shared_markets=shared_markets)
                    if _can_trade:
                        for opp in arb_opps[:3]:
                            if not self._running:
                                break
                            await self.arb.execute(opp, paper=paper)
                except Exception as e:
                    logger.error(f"[ARB] Errore strategia: {e}", exc_info=True)

                # ── 4. Data-Driven ──
                try:
                    predictions = await self.data.analyze(shared_markets=shared_markets)
                    if _can_trade:
                        for pred in predictions[:3]:
                            if not self._running:
                                break
                            signal = from_prediction(pred)
                            kelly = self.risk.kelly_size(pred.true_prob_yes, signal.price, "data_driven")
                            report = self.signal_validator.validate(signal, trade_size=kelly)
                            if report.result == ValidationResult.TRADE:
                                self.attribution.record_entry(
                                    pred.market.tokens.get(pred.best_side.lower(), ""),
                                    "data_driven", "data_driven", pred.market.category,
                                    edge_predicted=pred.best_edge, validation_score=report.score,
                                )
                                await self.data.execute(pred, paper=paper)
                except Exception as e:
                    logger.error(f"[DATA] Errore strategia: {e}", exc_info=True)

                # ── 5. Event-Driven ──
                try:
                    events = await self.event.scan(shared_markets=shared_markets)
                    if _can_trade:
                        for ev in events[:10]:  # v5.6: era [:5], tutte e 5 bloccate → prova 10
                            if not self._running:
                                break
                            signal = from_event_opportunity(ev)
                            kelly = self.risk.kelly_size(
                                min(signal.price + signal.edge, 0.95), signal.price, "event_driven"
                            )
                            report = self.signal_validator.validate(signal, trade_size=kelly)
                            if report.result == ValidationResult.TRADE or (
                                report.result == ValidationResult.REVIEW and signal.edge >= 0.04
                            ):
                                if report.result == ValidationResult.REVIEW:
                                    logger.info(f"[EVENT] REVIEW accepted: edge={signal.edge:.4f} >= 0.04")
                                self.attribution.record_entry(
                                    ev.market.tokens.get(ev.side.lower(), ""),
                                    "event_driven", getattr(ev, 'signal_type', 'structural'),
                                    getattr(ev, 'event_type', ''),
                                    edge_predicted=ev.edge, validation_score=report.score,
                                )
                                await self.event.execute(ev, paper=paper)
                except Exception as e:
                    logger.error(f"[EVENT] Errore strategia: {e}", exc_info=True)

                # ── 6. High-Probability Bonds (ogni 5 cicli — bond sono lenti) ──
                if self._cycle % 5 == 0:
                    try:
                        bond_opps = await self.bond.scan(shared_markets=shared_markets)
                        if _can_trade:
                            for opp in bond_opps[:3]:
                                if not self._running:
                                    break
                                signal = from_bond_opportunity(opp)
                                kelly = self.risk.kelly_size(
                                    min(signal.price + signal.edge, 0.99), opp.price_yes, "high_prob_bond"
                                )
                                report = self.signal_validator.validate(signal, trade_size=kelly)
                                if report.result == ValidationResult.TRADE or (
                                    report.result == ValidationResult.REVIEW and signal.edge >= 0.04
                                ):
                                    if report.result == ValidationResult.REVIEW:
                                        logger.info(f"[BOND] REVIEW accepted: edge={signal.edge:.4f} >= 0.04")
                                    self.attribution.record_entry(
                                        opp.market.tokens.get("yes", ""),
                                        "high_prob_bond", "bond", opp.market.category,
                                        edge_predicted=opp.edge, validation_score=report.score,
                                    )
                                    await self.bond.execute(opp, paper=paper)
                    except Exception as e:
                        logger.error(f"[BOND] Errore strategia: {e}", exc_info=True)

                # ── 7. Market Making — DISABILITATO v7.0 (necessita $2K+ budget) ──

                # ── 8. Whale Copy Trading (ogni 2 cicli — monitoraggio wallet) ──
                if self._cycle % 2 == 0:
                    try:
                        whale_opps = await self.whale.scan(shared_markets=shared_markets)
                        if _can_trade:
                            for opp in whale_opps[:3]:
                                if not self._running:
                                    break
                                signal = from_whale_opportunity(opp)
                                report = self.signal_validator.validate(signal, trade_size=opp.copy_size)
                                if report.result == ValidationResult.TRADE:
                                    self.attribution.record_entry(
                                        opp.market.tokens.get(opp.side.lower(), ""),
                                        "whale_copy", "whale_copy", opp.market.category,
                                        edge_predicted=opp.edge, validation_score=report.score,
                                    )
                                    await self.whale.execute(opp, paper=paper)
                    except Exception as e:
                        logger.error(f"[WHALE] Errore strategia: {e}", exc_info=True)

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
                                self.drift_detector.record_outcome(t.strategy, r["won"])
                                logger.info(
                                    f"[PNL] Trade chiuso: "
                                    f"{'VINTO' if r['won'] else 'PERSO'} "
                                    f"${pnl:+.2f} ({t.strategy}) "
                                    f"redeemed={r.get('redeemed', False)}"
                                )
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

                # ── 11. Purga posizioni stale (ogni 100 cicli) ──
                if self._cycle % 100 == 0:
                    self.risk.purge_stale_positions(max_age_hours=48.0)

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

                # ── v9.0: Drift + Calibration (ogni 500 cicli) ──
                if self._cycle % 500 == 0 and self._cycle > 0:
                    try:
                        drift_alerts = self.drift_detector.check_drift()
                        for alert in drift_alerts:
                            logger.warning(f"[DRIFT] {alert.message}")
                        suggestions = self.calibration.analyze()
                        for s in suggestions:
                            logger.info(f"[CALIBRATION] {s.reason}")
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

                # ── v9.0: Tail Risk (ogni 200 cicli) ──
                if self._cycle % 200 == 0 and self._cycle > 0:
                    try:
                        tail_report = self.tail_risk.analyze()
                        if tail_report.risk_level == "CRITICAL":
                            logger.warning(
                                f"[TAIL_RISK] CRITICAL: max loss "
                                f"${abs(tail_report.max_loss_scenario):.2f} "
                                f"({tail_report.exposure_pct:.0%} capitale)"
                            )
                    except Exception as e:
                        logger.warning(f"[v9.0] Errore tail risk: {e}")

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

                        self._resolved_cache.add(matched_mid)
                        data_api_resolved_mids.add(matched_mid)

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

                if resp.status_code == 404:
                    # Mercato non trovato su Gamma — prova endpoint CLOB
                    logger.debug(f"[PNL] Mercato {mid} non trovato su Gamma API")
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

                self._resolved_cache.add(mid)

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

            # Calcola PnL percentuale
            if trade.price > 0 and current_bid > 0:
                pnl_pct = (current_bid - trade.price) / trade.price
            elif current_bid == 0:
                pnl_pct = -1.0
            else:
                pnl_pct = 0.0

            # v8.0: Bid sanity check — se bid < 50% dell'entry, l'order book
            # è probabilmente vuoto/stale. Non triggerare stop loss su dati fantasma.
            if current_bid > 0 and trade.price > 0:
                bid_ratio = current_bid / trade.price
                if bid_ratio < 0.50:
                    logger.debug(
                        f"[POSITION-MGR] Bid sospetto: {trade.strategy} "
                        f"entry@{trade.price:.4f} bid@{current_bid:.4f} "
                        f"(ratio={bid_ratio:.1%}) — SKIP (order book vuoto?)"
                    )
                    held += 1
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
                # v7.4: Recupera shares REALI dal CLOB per evitare over-sell
                fill_info = self.api.get_last_fill(trade.token_id, side="BUY")
                if fill_info and fill_info["fill_size"] > 0:
                    shares = fill_info["fill_size"]
                    real_entry = fill_info["fill_price"]
                else:
                    shares = trade.size / trade.price if trade.price > 0 else 0
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

    async def _dashboard_loop(self):
        while self._running:
            await asyncio.sleep(20)
            if self.binance.price > 0:
                dashboard(
                    self.risk, self.config.paper_trading, self._cycle,
                    unrealized_pnl=getattr(self, '_unrealized_pnl', None),
                    usdc_balance=getattr(self, '_usdc_balance', None),
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
