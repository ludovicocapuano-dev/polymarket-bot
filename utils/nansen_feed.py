"""
Feed Nansen API — Smart Money tracking per crypto strategies.

Nansen fornisce dati sullo "smart money": wallet di fondi, trader
profittevoli, whale, exchange flows. Questi dati rivelano cosa fanno
i trader piu' profittevoli PRIMA che il prezzo si muova.

Nansen API:
- Base URL: https://api.nansen.ai
- Auth: header apiKey
- Tutti gli endpoint sono POST con body JSON
- Rate limit: 20 req/s, 500 req/min

Endpoint chiave:
1. /api/v1/smart-money/netflow — accumulazione/distribuzione smart money per token
2. /api/v1/tgm/flow-intelligence — flussi per segmento (whale, smart money, exchange)
3. /api/v1/smart-money/dex-trades — trade DEX in tempo reale da smart trader

Metriche chiave:
- Smart Money Net Flow: positivo = accumulazione (bullish), negativo = distribuzione (bearish)
- Whale Net Flow: grandi wallet che accumulano/distribuiscono
- Exchange Net Flow: flussi da/verso exchange (complementare a CryptoQuant)
- Trader Count: quanti smart trader stanno tradando un token (volume segnale)

Uso nel bot:
- data_driven: smart money flow come segnale addizionale per probability estimation
- crypto_5min: smart money activity come conferma direzionale
"""

import logging
import os
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.nansen.ai"

# Token addresses per chain (Ethereum)
# Per smart-money/netflow usiamo i nomi catena, non gli address
TOKEN_SYMBOLS: dict[str, dict] = {
    "btc": {
        "chains": ["ethereum", "bitcoin"],
        "name": "Bitcoin",
        "wbtc_address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    },
    "eth": {
        "chains": ["ethereum"],
        "name": "Ethereum",
        "weth_address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    },
    "sol": {
        "chains": ["solana"],
        "name": "Solana",
    },
    "xrp": {
        "chains": [],  # Non su EVM, supporto limitato
        "name": "XRP",
    },
}

# Cache 5 minuti — smart money non cambia ogni secondo
CACHE_TTL = 300


@dataclass
class SmartMoneyFlow:
    """Dati smart money per un token/asset."""
    symbol: str = ""

    # Net flows (in USD) da smart money per diversi timeframe
    net_flow_24h: float = 0.0       # Net flow ultimi 24h
    net_flow_7d: float = 0.0        # Net flow ultimi 7 giorni
    net_flow_30d: float = 0.0       # Net flow ultimi 30 giorni

    # Conteggi trader
    smart_trader_count: int = 0     # Numero di smart trader attivi
    whale_count: int = 0            # Numero di whale attive
    fund_count: int = 0             # Numero di fondi attivi

    # Flow Intelligence (per segmento)
    whale_net_flow: float = 0.0     # Net flow whale
    exchange_net_flow: float = 0.0  # Net flow exchange
    smart_trader_net_flow: float = 0.0  # Net flow smart trader
    top_pnl_net_flow: float = 0.0   # Net flow dei trader piu' profittevoli

    fetched_at: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.fetched_at) < CACHE_TTL if self.fetched_at > 0 else False

    @property
    def smart_money_signal(self) -> str:
        """
        Segnale basato su smart money net flow.
        STRONG_BUY: net flow 24h molto positivo + trader count alto
        MILD_BUY: net flow positivo
        STRONG_SELL: net flow molto negativo
        MILD_SELL: net flow negativo
        NEUTRAL: net flow vicino a zero
        """
        if not self.is_fresh:
            return "UNKNOWN"

        nf = self.net_flow_24h

        # Soglie adattive basate sul numero di trader
        # Piu' trader = segnale piu' affidabile
        trader_weight = max(self.smart_trader_count, 1)

        if nf > 1_000_000 and trader_weight >= 10:
            return "STRONG_BUY"
        elif nf > 100_000:
            return "MILD_BUY"
        elif nf < -1_000_000 and trader_weight >= 10:
            return "STRONG_SELL"
        elif nf < -100_000:
            return "MILD_SELL"
        return "NEUTRAL"

    @property
    def smart_money_direction(self) -> float:
        """
        Segnale numerico: -1.0 (forte vendita smart money) a +1.0 (forte acquisto).
        """
        if not self.is_fresh:
            return 0.0

        signal = self.smart_money_signal
        return {
            "STRONG_BUY": 1.0,
            "MILD_BUY": 0.5,
            "NEUTRAL": 0.0,
            "MILD_SELL": -0.5,
            "STRONG_SELL": -1.0,
            "UNKNOWN": 0.0,
        }.get(signal, 0.0)

    @property
    def trend_consistency(self) -> float:
        """
        Quanto e' consistente il trend: 0.0 (incoerente) a 1.0 (molto coerente).
        Se 24h e 7d concordano = coerente. Se divergono = incoerente.
        """
        if not self.is_fresh or self.net_flow_7d == 0:
            return 0.0

        # Stesso segno = coerente
        if (self.net_flow_24h > 0 and self.net_flow_7d > 0) or \
           (self.net_flow_24h < 0 and self.net_flow_7d < 0):
            return min(abs(self.net_flow_24h / self.net_flow_7d) * 3, 1.0)
        else:
            # Segno opposto = inversione
            return 0.0

    @property
    def multi_segment_agreement(self) -> float:
        """
        Quanti segmenti concordano sulla direzione: 0.0 a 1.0.
        Se whale + smart trader + exchange tutti comprano = 1.0.
        """
        if not self.is_fresh:
            return 0.0

        segments = [
            self.whale_net_flow,
            self.smart_trader_net_flow,
            self.exchange_net_flow,
            self.top_pnl_net_flow,
        ]
        # Conta segmenti non-zero
        active = [s for s in segments if abs(s) > 10_000]
        if not active:
            return 0.0

        # Conta quanti sono nella stessa direzione
        pos = sum(1 for s in active if s > 0)
        neg = sum(1 for s in active if s < 0)
        majority = max(pos, neg)
        return majority / len(active)


@dataclass
class NansenFeed:
    """
    Client per Nansen API — Smart Money tracking.

    Fetcha dati su accumulazione/distribuzione smart money per BTC, ETH, SOL.
    Degrada gracefully se API key manca o API irraggiungibile.
    """
    _api_key: str = ""
    _cache: dict[str, SmartMoneyFlow] = field(default_factory=dict)
    _session: requests.Session | None = None
    _available: bool | None = None
    _discovered_endpoints: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self._api_key = os.environ.get("NANSEN_API_KEY", "")
        if not self._api_key:
            logger.info("[NANSEN] Nessuna API key — smart money tracking disabilitato")
            self._available = False
            return

        self._session = requests.Session()
        self._session.headers.update({
            "apiKey": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "PolymarketBot/4.0",
        })

        # Init cache per simboli supportati
        for sym in TOKEN_SYMBOLS:
            self._cache[sym] = SmartMoneyFlow(symbol=sym)

        logger.info(
            f"[NANSEN] Feed inizializzato — "
            f"smart money tracking per {', '.join(s.upper() for s in TOKEN_SYMBOLS)}"
        )

    # ── Accesso dati ─────────────────────────────────────────────

    def get_smart_money(self, symbol: str = "btc") -> SmartMoneyFlow:
        """
        Ottieni dati smart money per un simbolo.
        Se in cache e freschi, non chiama l'API.
        """
        sym = symbol.lower()
        if sym not in TOKEN_SYMBOLS:
            return SmartMoneyFlow(symbol=sym)

        cached = self._cache.get(sym)
        if cached and cached.is_fresh:
            return cached

        self._fetch_all(sym)
        return self._cache.get(sym, SmartMoneyFlow(symbol=sym))

    def smart_money_summary(self) -> str:
        """Stringa riassuntiva per il log."""
        parts = []
        for sym in ["btc", "eth", "sol"]:
            sm = self._cache.get(sym)
            if sm and sm.is_fresh:
                parts.append(
                    f"{sym.upper()}: {sm.smart_money_signal} "
                    f"NF24h=${sm.net_flow_24h/1e6:+.1f}M "
                    f"({sm.smart_trader_count} traders)"
                )
        return " | ".join(parts) if parts else "SmartMoney: --"

    # ── Fetch aggregato ──────────────────────────────────────────

    def _fetch_all(self, symbol: str):
        """Fetcha dati smart money per un simbolo."""
        if self._available is False or not self._session:
            return

        data = self._cache.get(symbol, SmartMoneyFlow(symbol=symbol))
        token_info = TOKEN_SYMBOLS.get(symbol, {})
        chains = token_info.get("chains", [])

        if not chains:
            data.fetched_at = time.time()
            self._cache[symbol] = data
            return

        # 1. Smart Money Netflow
        netflow_data = self._fetch_smart_money_netflow(chains)
        if netflow_data:
            self._parse_netflow(data, netflow_data, symbol)

        # 2. Flow Intelligence (se abbiamo un token address)
        token_addr = token_info.get("wbtc_address") or token_info.get("weth_address")
        chain = chains[0] if chains else "ethereum"
        if token_addr:
            flow_intel = self._fetch_flow_intelligence(chain, token_addr)
            if flow_intel:
                self._parse_flow_intelligence(data, flow_intel)

        data.fetched_at = time.time()
        self._cache[symbol] = data

        if data.net_flow_24h != 0 or data.smart_trader_count > 0:
            logger.debug(
                f"[NANSEN] {symbol.upper()}: {data.smart_money_signal} "
                f"NF24h=${data.net_flow_24h/1e6:+.1f}M "
                f"NF7d=${data.net_flow_7d/1e6:+.1f}M "
                f"Traders={data.smart_trader_count}"
            )

    # ── API Calls ────────────────────────────────────────────────

    def _fetch_smart_money_netflow(self, chains: list[str]) -> dict | None:
        """
        POST /api/v1/smart-money/netflow

        Ritorna i net flow aggregati smart money per catena.
        """
        if not self._session:
            return None

        # Prova endpoint v1, poi beta
        for endpoint in ["/api/v1/smart-money/netflow", "/api/beta/smart-money/netflow"]:
            body = {
                "chains": chains,
                "filters": {
                    "include_smart_money_labels": [
                        "Smart Trader (30D)",
                        "Smart Trader (90D)",
                        "Fund",
                    ],
                },
                "pagination": {
                    "page": 1,
                    "per_page": 10,
                },
                "order_by": [
                    {"field": "net_flow_24h_usd", "direction": "DESC"}
                ],
            }

            result = self._do_post(endpoint, body)
            if result is not None:
                return result

        return None

    def _fetch_flow_intelligence(self, chain: str, token_address: str) -> dict | None:
        """
        POST /api/v1/tgm/flow-intelligence

        Ritorna flussi per segmento (whale, smart money, exchange, etc.)
        """
        if not self._session:
            return None

        for endpoint in ["/api/v1/tgm/flow-intelligence", "/api/beta/tgm/flow-intelligence"]:
            body = {
                "chain": chain,
                "token_address": token_address,
                "timeframe": "24h",
            }

            result = self._do_post(endpoint, body)
            if result is not None:
                return result

        return None

    def _do_post(self, endpoint: str, body: dict) -> dict | None:
        """Esegui una chiamata POST."""
        if not self._session or self._available is False:
            return None

        url = f"{BASE_URL}{endpoint}"

        try:
            resp = self._session.post(url, json=body, timeout=12)

            if resp.status_code == 200:
                data = resp.json()
                if self._available is None:
                    self._available = True
                    logger.info(f"[NANSEN] API connessa via {endpoint}")
                return data

            elif resp.status_code == 401 or resp.status_code == 403:
                logger.warning(f"[NANSEN] Auth fallita ({resp.status_code})")
                self._available = False
                return None

            elif resp.status_code == 429:
                logger.warning("[NANSEN] Rate limit raggiunto (429)")
                return None

            elif resp.status_code == 402:
                # Crediti insufficienti
                logger.warning("[NANSEN] Crediti API insufficienti (402)")
                return None

            else:
                logger.debug(f"[NANSEN] HTTP {resp.status_code} su {endpoint}")
                return None

        except requests.Timeout:
            logger.debug(f"[NANSEN] Timeout su {endpoint}")
            return None
        except requests.RequestException as e:
            logger.debug(f"[NANSEN] Errore: {e}")
            return None

    # ── Parsing ──────────────────────────────────────────────────

    def _parse_netflow(self, data: SmartMoneyFlow, response: dict, symbol: str):
        """
        Parsa la risposta di /smart-money/netflow.

        La risposta puo' contenere diversi token.
        Cerchiamo quello che corrisponde al nostro simbolo.
        """
        items = self._extract_data_list(response)
        if not items:
            return

        token_name = TOKEN_SYMBOLS.get(symbol, {}).get("name", "").lower()

        for item in items:
            # Match per nome o simbolo
            item_name = str(item.get("token_name", item.get("name", ""))).lower()
            item_symbol = str(item.get("token_symbol", item.get("symbol", ""))).lower()

            if symbol in item_symbol or token_name in item_name or \
               item_symbol in (symbol, f"w{symbol}"):
                data.net_flow_24h = self._safe_float(item, "net_flow_24h_usd",
                                   self._safe_float(item, "net_flow_24h", 0.0))
                data.net_flow_7d = self._safe_float(item, "net_flow_7d_usd",
                                  self._safe_float(item, "net_flow_7d", 0.0))
                data.net_flow_30d = self._safe_float(item, "net_flow_30d_usd",
                                   self._safe_float(item, "net_flow_30d", 0.0))
                data.smart_trader_count = self._safe_int(item, "trader_count",
                                         self._safe_int(item, "smart_trader_count", 0))
                data.whale_count = self._safe_int(item, "whale_count", 0)
                data.fund_count = self._safe_int(item, "fund_count", 0)
                return

        # Se non troviamo un match esatto, usa il primo risultato se c'e' un solo token
        if len(items) == 1:
            item = items[0]
            data.net_flow_24h = self._safe_float(item, "net_flow_24h_usd", 0.0)
            data.net_flow_7d = self._safe_float(item, "net_flow_7d_usd", 0.0)
            data.smart_trader_count = self._safe_int(item, "trader_count", 0)

    def _parse_flow_intelligence(self, data: SmartMoneyFlow, response: dict):
        """
        Parsa la risposta di /tgm/flow-intelligence.

        Contiene flussi per segmento: whale, smart_trader, exchange, top_pnl, etc.
        """
        payload = response
        if "data" in response and isinstance(response["data"], dict):
            payload = response["data"]
        elif "result" in response and isinstance(response["result"], dict):
            payload = response["result"]

        # Se payload e' una lista, prendi il primo elemento
        if isinstance(payload, list) and payload:
            payload = payload[0]

        if not isinstance(payload, dict):
            return

        data.whale_net_flow = self._safe_float(payload, "whale_net_flow_usd",
                             self._safe_float(payload, "whale_net_flow", 0.0))
        data.smart_trader_net_flow = self._safe_float(payload, "smart_trader_net_flow_usd",
                                    self._safe_float(payload, "smart_trader_net_flow", 0.0))
        data.exchange_net_flow = self._safe_float(payload, "exchange_net_flow_usd",
                               self._safe_float(payload, "exchange_net_flow", 0.0))
        data.top_pnl_net_flow = self._safe_float(payload, "top_pnl_net_flow_usd",
                               self._safe_float(payload, "top_pnl_net_flow", 0.0))

        # Aggiorna conteggi se presenti
        wc = self._safe_int(payload, "whale_wallet_count", 0)
        if wc > 0:
            data.whale_count = wc
        stc = self._safe_int(payload, "smart_trader_wallet_count", 0)
        if stc > 0:
            data.smart_trader_count = max(data.smart_trader_count, stc)

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _extract_data_list(response: dict) -> list[dict]:
        """Estrai la lista di dati dalla risposta."""
        if isinstance(response, list):
            return response

        for key in ["data", "result", "results", "tokens", "items"]:
            val = response.get(key)
            if isinstance(val, list):
                return val
            elif isinstance(val, dict):
                # Potrebbe contenere una sottolista
                for subkey in ["data", "items", "tokens", "rows"]:
                    subval = val.get(subkey)
                    if isinstance(subval, list):
                        return subval
        return []

    @staticmethod
    def _safe_float(d: dict, key: str, default: float = 0.0) -> float:
        val = d.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_int(d: dict, key: str, default: int = 0) -> int:
        val = d.get(key)
        if val is None:
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default
