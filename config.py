"""
Configurazione centralizzata del toolkit.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class PolymarketCreds:
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    private_key: str = ""
    funder_address: str = ""
    host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137


@dataclass
class RiskConfig:
    total_capital: float = 1000.0
    max_bet_size: float = 40.0   # v8.0: da $25 a $40 (Becker sweet spot: $100-$1K)
    max_bet_percent: float = 8.0
    max_daily_loss: float = 150.0
    min_edge: float = 0.04
    kelly_fraction: float = 0.25
    max_consecutive_losses: int = 10
    max_open_positions: int = 30  # v7.3: aumentato da 20 (Casino Model — più diversificazione)
    reserve_floor_pct: float = 25.0  # v8.1: % capitale da tenere sempre liquido


@dataclass
class AllocationConfig:
    """
    v8.0: Riallocazione data-driven (Becker Dataset analysis).
    - event_driven 10→15: politics è la categoria più profittevole
    - whale_copy 5→10: Becker conferma che copiare whale funziona (68% WR)
    - data_driven 10→5: crypto ben calibrato, poco edge disponibile
    - weather 20→15: redistribuiamo dove c'è più edge
    """
    crypto_5min: int = 0       # ELIMINATO: Kelly -0.22, fees > edge
    weather: int = 15          # v8.0: ridotto da 20 — buono ma meno edge di politics
    arbitrage: int = 10        # Invariato
    data_driven: int = 5       # v8.0: ridotto da 10 — crypto ben calibrato
    event_driven: int = 15     # v8.0: aumentato da 10 — politics edge forte
    arb_gabagool: int = 20     # Invariato: risk-free
    high_prob_bond: int = 25   # Invariato: confermato dal backtest
    market_making: int = 0     # ELIMINATO: necessita $2K+ budget
    whale_copy: int = 10       # v8.0: aumentato da 5 — whale hanno edge reale


@dataclass
class Config:
    creds: PolymarketCreds = field(default_factory=PolymarketCreds)
    risk: RiskConfig = field(default_factory=RiskConfig)
    allocation: AllocationConfig = field(default_factory=AllocationConfig)
    paper_trading: bool = True
    poll_interval: int = 3
    log_level: str = "INFO"
    # v9.0: Storage layer
    db_dsn: str = ""        # PostgreSQL DSN (es. postgresql://localhost/polymarket_bot)
    redis_url: str = ""     # Redis URL (es. redis://localhost:6379)

    @classmethod
    def from_env(cls) -> "Config":
        creds = PolymarketCreds(
            api_key=os.getenv("POLYMARKET_API_KEY", ""),
            api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
            api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
            private_key=os.getenv("PRIVATE_KEY", ""),
            funder_address=os.getenv("FUNDER_ADDRESS", ""),
        )
        risk = RiskConfig(
            total_capital=float(os.getenv("TOTAL_CAPITAL", "1000")),
            max_bet_size=float(os.getenv("MAX_BET_SIZE", "25")),
            max_bet_percent=float(os.getenv("MAX_BET_PERCENT", "8")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "150")),
            max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "10")),
            min_edge=float(os.getenv("MIN_EDGE_THRESHOLD", "0.04")),
            max_open_positions=int(float(os.getenv("MAX_OPEN_POSITIONS", "30"))),
        )
        # Se le nuove strategie non hanno env vars, usa i nuovi default
        # ignorando le vecchie allocazioni dal .env
        has_new_alloc = any(os.getenv(k) for k in [
            "ALLOC_HIGH_PROB_BOND", "ALLOC_MARKET_MAKING", "ALLOC_WHALE_COPY"
        ])
        if has_new_alloc:
            # Tutte le allocazioni sono esplicite nel .env
            alloc = AllocationConfig(
                crypto_5min=int(os.getenv("ALLOC_CRYPTO_5MIN", "0")),
                weather=int(os.getenv("ALLOC_WEATHER", "20")),
                arbitrage=int(os.getenv("ALLOC_ARBITRAGE", "10")),
                data_driven=int(os.getenv("ALLOC_DATA_DRIVEN", "10")),
                event_driven=int(os.getenv("ALLOC_EVENT_DRIVEN", "10")),
                arb_gabagool=int(os.getenv("ALLOC_ARB_GABAGOOL", "20")),
                high_prob_bond=int(os.getenv("ALLOC_HIGH_PROB_BOND", "25")),
                market_making=int(os.getenv("ALLOC_MARKET_MAKING", "0")),
                whale_copy=int(os.getenv("ALLOC_WHALE_COPY", "5")),
            )
        else:
            # .env ha solo le vecchie 6 strategie — usa i nuovi default v6.0
            alloc = AllocationConfig()
        return cls(
            creds=creds,
            risk=risk,
            allocation=alloc,
            paper_trading=os.getenv("PAPER_TRADING", "true").lower() == "true",
            poll_interval=int(os.getenv("POLL_INTERVAL", "3")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            # v9.0: Storage layer (graceful: se vuoto, usa JSON come prima)
            db_dsn=os.getenv("DATABASE_DSN", ""),
            redis_url=os.getenv("REDIS_URL", ""),
        )

    def validate(self) -> list[str]:
        errors = []
        if not self.creds.api_key:
            errors.append("POLYMARKET_API_KEY mancante")
        if not self.creds.private_key:
            errors.append("PRIVATE_KEY mancante")
        if self.risk.total_capital <= 0:
            errors.append("TOTAL_CAPITAL deve essere > 0")
        s = (self.allocation.crypto_5min + self.allocation.weather +
             self.allocation.arbitrage + self.allocation.data_driven +
             self.allocation.event_driven + self.allocation.arb_gabagool +
             self.allocation.high_prob_bond + self.allocation.market_making +
             self.allocation.whale_copy)
        if s != 100:
            errors.append(f"Allocazione deve sommare a 100 (attuale: {s})")
        return errors

    def capital_for(self, strategy: str) -> float:
        pct_map = {
            "crypto_5min": self.allocation.crypto_5min,
            "weather": self.allocation.weather,
            "arbitrage": self.allocation.arbitrage,
            "data_driven": self.allocation.data_driven,
            "event_driven": self.allocation.event_driven,
            "arb_gabagool": self.allocation.arb_gabagool,
            "high_prob_bond": self.allocation.high_prob_bond,
            "market_making": self.allocation.market_making,
            "whale_copy": self.allocation.whale_copy,
        }
        pct = pct_map.get(strategy, 0)
        return self.risk.total_capital * (pct / 100.0)
