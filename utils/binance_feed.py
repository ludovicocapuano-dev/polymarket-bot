"""
Feed real-time multi-crypto da Binance via WebSocket.
Supporta BTC, ETH, SOL, XRP su USDT via combined stream.

Architettura v5.9:
- Un solo WebSocket in modalita' combined stream
- Trade stream (@trade) per prezzo real-time
- Depth stream (@depth5@1000ms) per order book top-5 livelli
- Order Book Imbalance (OBI) calcolato dal depth stream
- Trade flow tracking (buy/sell pressure da aggressor side)
- Retrocompatibile: .price, .history, .momentum() etc. restituiscono BTC

URL combined stream:
  wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@depth5@1000ms/...
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import websockets

logger = logging.getLogger(__name__)

# ── Simboli supportati ─────────────────────────────────────────
SUPPORTED_SYMBOLS: dict[str, str] = {
    "btc": "btcusdt",
    "eth": "ethusdt",
    "sol": "solusdt",
    "xrp": "xrpusdt",
}

BINANCE_COMBINED_WS = (
    "wss://stream.binance.com:9443/stream?streams="
    + "/".join(
        f"{pair}@trade/{pair}@depth5@1000ms"
        for pair in SUPPORTED_SYMBOLS.values()
    )
)


@dataclass
class DepthSnapshot:
    """Snapshot order book top-5 livelli."""
    bids: list[tuple[float, float]] = field(default_factory=list)  # [(price, qty), ...]
    asks: list[tuple[float, float]] = field(default_factory=list)
    updated_at: float = 0.0


@dataclass
class SymbolData:
    """Dati real-time per un singolo simbolo."""
    price: float = 0.0
    updated_at: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=2000))

    # Order book depth (top 5 livelli)
    depth: DepthSnapshot = field(default_factory=DepthSnapshot)

    # OBI (Order Book Imbalance) history: deque di (timestamp, obi_value)
    # OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol), range [-1, +1]
    obi_history: deque = field(default_factory=lambda: deque(maxlen=300))

    # Trade flow: buy/sell volume aggregato per finestra temporale
    # Ogni entry: (timestamp, price, qty, is_buyer_maker)
    trade_flow: deque = field(default_factory=lambda: deque(maxlen=5000))


@dataclass
class BinanceFeed:
    """
    Feed multi-crypto da Binance con Order Book Imbalance.

    Retrocompatibile: .price, .history etc. restituiscono dati BTC.
    Per altri simboli: usa il parametro symbol="eth" etc.
    """
    _symbols: dict[str, SymbolData] = field(default_factory=dict)
    _running: bool = False
    _pair_to_sym: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        for sym in SUPPORTED_SYMBOLS:
            self._symbols[sym] = SymbolData()
        self._pair_to_sym = {v: k for k, v in SUPPORTED_SYMBOLS.items()}

    # ── Proprieta' retrocompatibili (BTC) ──────────────────────

    @property
    def price(self) -> float:
        """Prezzo BTC (retrocompatibile con data_driven)."""
        return self._symbols["btc"].price

    @property
    def updated_at(self) -> float:
        return self._symbols["btc"].updated_at

    @property
    def history(self) -> deque:
        return self._symbols["btc"].history

    # ── Accesso per simbolo ────────────────────────────────────

    def get_symbol(self, symbol: str) -> SymbolData:
        """Ottieni dati di un simbolo. Ritorna SymbolData vuoto se non esiste."""
        return self._symbols.get(symbol.lower(), SymbolData())

    def symbol_price(self, symbol: str) -> float:
        """Prezzo corrente di un simbolo."""
        return self.get_symbol(symbol).price

    def ready_symbols(self) -> list[str]:
        """Lista dei simboli con feed pronto (price > 0)."""
        return [sym for sym, sd in self._symbols.items() if sd.price > 0]

    def prices_summary(self) -> str:
        """Stringa riassuntiva dei prezzi per il log."""
        parts = []
        for sym in SUPPORTED_SYMBOLS:
            sd = self._symbols[sym]
            if sd.price > 0:
                parts.append(f"{sym.upper()}=${sd.price:,.2f}")
            else:
                parts.append(f"{sym.upper()}=--")
        return " | ".join(parts)

    # ── WebSocket ──────────────────────────────────────────────

    async def connect(self):
        """Connessione al combined stream multi-crypto (trade + depth)."""
        self._running = True

        while self._running:
            try:
                async with websockets.connect(
                    BINANCE_COMBINED_WS, ping_interval=20
                ) as ws:
                    symbols_str = ", ".join(s.upper() for s in SUPPORTED_SYMBOLS)
                    logger.info(
                        f"Binance feed multi-crypto connesso ({symbols_str}) "
                        f"[trade + depth5]"
                    )
                    async for msg in ws:
                        if not self._running:
                            break
                        raw = json.loads(msg)

                        # Combined stream format:
                        # {"stream": "btcusdt@trade", "data": {...}}
                        # {"stream": "btcusdt@depth5@1000ms", "data": {...}}
                        stream_name = raw.get("stream", "")
                        data = raw.get("data", raw)

                        if "@trade" in stream_name:
                            self._handle_trade(stream_name, data)
                        elif "@depth" in stream_name:
                            self._handle_depth(stream_name, data)

            except websockets.ConnectionClosed:
                logger.warning("Binance disconnesso, riconnessione...")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Errore Binance: {e}")
                await asyncio.sleep(5)

    def _handle_trade(self, stream_name: str, data: dict):
        """Gestisce messaggio @trade: aggiorna prezzo + trade flow."""
        pair = stream_name.split("@")[0]
        sym = self._pair_to_sym.get(pair)
        if not sym or sym not in self._symbols:
            return

        sd = self._symbols[sym]
        price = float(data["p"])
        qty = float(data["q"])
        is_buyer_maker = data.get("m", False)  # True = seller aggressor (sell pressure)

        sd.price = price
        sd.updated_at = time.time()
        sd.history.append((sd.updated_at, price))

        # Trade flow: is_buyer_maker=True means the buyer was the maker,
        # so the trade was initiated by a SELLER (sell pressure).
        # is_buyer_maker=False means trade initiated by BUYER (buy pressure).
        sd.trade_flow.append((sd.updated_at, price, qty, is_buyer_maker))

    def _handle_depth(self, stream_name: str, data: dict):
        """Gestisce messaggio @depth5: aggiorna order book + calcola OBI."""
        pair = stream_name.split("@")[0]
        sym = self._pair_to_sym.get(pair)
        if not sym or sym not in self._symbols:
            return

        sd = self._symbols[sym]
        now = time.time()

        # Parse bids e asks (top 5 livelli)
        # Format: {"bids": [["price","qty"], ...], "asks": [["price","qty"], ...]}
        bids_raw = data.get("bids", [])
        asks_raw = data.get("asks", [])

        sd.depth.bids = [(float(b[0]), float(b[1])) for b in bids_raw[:5]]
        sd.depth.asks = [(float(a[0]), float(a[1])) for a in asks_raw[:5]]
        sd.depth.updated_at = now

        # Calcola OBI (Order Book Imbalance) dai top-5 livelli
        # OBI = (sum_bid_vol - sum_ask_vol) / (sum_bid_vol + sum_ask_vol)
        # Range: [-1, +1]. Positivo = piu' bid che ask = pressione d'acquisto
        bid_vol = sum(qty for _, qty in sd.depth.bids) if sd.depth.bids else 0
        ask_vol = sum(qty for _, qty in sd.depth.asks) if sd.depth.asks else 0
        total = bid_vol + ask_vol

        if total > 0:
            obi = (bid_vol - ask_vol) / total
        else:
            obi = 0.0

        sd.obi_history.append((now, obi))

    # ── Order Book Imbalance (OBI) ────────────────────────────

    def obi(self, symbol: str = "btc") -> float:
        """
        OBI corrente (ultimo valore).
        Positivo = bid pressure (bullish), Negativo = ask pressure (bearish).
        """
        sd = self.get_symbol(symbol)
        if sd.obi_history:
            return sd.obi_history[-1][1]
        return 0.0

    def obi_avg(self, seconds: int = 10, symbol: str = "btc") -> float:
        """Media OBI negli ultimi N secondi."""
        sd = self.get_symbol(symbol)
        if not sd.obi_history:
            return 0.0
        now = time.time()
        values = [v for ts, v in sd.obi_history if now - ts <= seconds]
        if not values:
            return 0.0
        return sum(values) / len(values)

    def obi_trend(self, symbol: str = "btc") -> float:
        """
        Trend dell'OBI: confronta media ultimi 5s vs 5s precedenti.
        Positivo = OBI in crescita (bid pressure in aumento).
        """
        sd = self.get_symbol(symbol)
        if len(sd.obi_history) < 5:
            return 0.0
        now = time.time()
        recent = [v for ts, v in sd.obi_history if now - ts <= 5]
        older = [v for ts, v in sd.obi_history if 5 < now - ts <= 10]
        if not recent or not older:
            return 0.0
        return (sum(recent) / len(recent)) - (sum(older) / len(older))

    # ── Trade Flow Imbalance (TFI) ────────────────────────────

    def trade_flow_imbalance(
        self, seconds: int = 30, symbol: str = "btc"
    ) -> float:
        """
        Trade Flow Imbalance: rapporto buy/sell volume dagli ultimi N secondi.
        Basato sull'aggressor side dei trade.

        Positivo = piu' buy aggressor (bullish).
        Negativo = piu' sell aggressor (bearish).
        Range: [-1, +1]
        """
        sd = self.get_symbol(symbol)
        if not sd.trade_flow:
            return 0.0

        now = time.time()
        buy_vol = 0.0
        sell_vol = 0.0

        for ts, price, qty, is_buyer_maker in sd.trade_flow:
            if now - ts > seconds:
                continue
            dollar_vol = price * qty
            if is_buyer_maker:
                # Buyer was maker → seller initiated → sell pressure
                sell_vol += dollar_vol
            else:
                # Seller was maker → buyer initiated → buy pressure
                buy_vol += dollar_vol

        total = buy_vol + sell_vol
        if total == 0:
            return 0.0
        return (buy_vol - sell_vol) / total

    def depth_available(self, symbol: str = "btc") -> bool:
        """Controlla se abbiamo dati depth per un simbolo."""
        sd = self.get_symbol(symbol)
        return sd.depth.updated_at > 0 and len(sd.obi_history) > 0

    # ── Analisi tecnica (con supporto multi-simbolo) ───────────

    def momentum(self, seconds: int = 30, symbol: str = "btc") -> float:
        """Variazione percentuale negli ultimi N secondi."""
        sd = self.get_symbol(symbol)
        if not sd.history or sd.price == 0:
            return 0.0
        now = time.time()
        for ts, p in reversed(sd.history):
            if now - ts >= seconds:
                return (sd.price - p) / p if p > 0 else 0.0
        return 0.0

    def volatility(self, seconds: int = 60, symbol: str = "btc") -> float:
        """Deviazione standard dei rendimenti."""
        sd = self.get_symbol(symbol)
        now = time.time()
        prices = [p for ts, p in sd.history if now - ts <= seconds]
        if len(prices) < 5:
            return 0.0
        rets = [
            (prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        ]
        if not rets:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return var ** 0.5

    def direction_confidence(
        self, symbol: str = "btc"
    ) -> tuple[str, float]:
        """Direzione del prezzo e confidenza (0-1)."""
        m5 = self.momentum(5, symbol)
        m15 = self.momentum(15, symbol)
        m30 = self.momentum(30, symbol)
        weighted = m5 * 0.5 + m15 * 0.3 + m30 * 0.2
        vol = self.volatility(60, symbol)
        if vol == 0:
            return "FLAT", 0.0
        strength = abs(weighted) / max(vol, 1e-8)
        conf = min(strength / 3.0, 1.0)
        if weighted > 0.0001:
            return "UP", conf
        elif weighted < -0.0001:
            return "DOWN", conf
        return "FLAT", 0.0

    async def stop(self):
        self._running = False
