"""
Feed CryptoQuant API v1 — On-chain analytics per crypto strategies.

Fornisce dati on-chain (exchange flows, MVRV, SOPR) per BTC e ETH.
Questi dati rappresentano il comportamento di whale e istituzioni
PRIMA che si manifesti nel prezzo.

CryptoQuant API v1:
- Base URL: https://api.cryptoquant.com/v1
- Auth: Authorization: Bearer <key>
- Response: {"status": {...}, "result": {"data": [...]}}

Metriche chiave per il trading:
- Exchange Netflow: Inflow - Outflow. Positivo = depositi (bearish),
  Negativo = prelievi (bullish, accumulazione).
- MVRV Ratio: Market Value / Realized Value.
  > 3.7 = sopravvalutato (top). < 1.0 = sottovalutato (bottom).
- SOPR: Spent Output Profit Ratio.
  > 1 = venditori in profitto. < 1 = venditori in perdita.
- Exchange Whale Ratio: % di flussi whale sul totale.
  Alto = whale attive (volatilita' in arrivo).

Uso nel bot:
- crypto_5min: netflow come conferma direzionale
- data_driven: MVRV per stimare se il prezzo e' sopra/sottovalutato
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.cryptoquant.com/v1"

# Endpoint che proviamo (in ordine di priorita')
# CryptoQuant cambia naming convention tra versioni
ENDPOINT_VARIANTS = {
    "netflow": [
        "/btc/exchange-flows/netflow",
        "/btc/exchange-flows/exchange-netflow-total",
        "/btc/exchange-flows/netflow-total",
    ],
    "inflow": [
        "/btc/exchange-flows/inflow",
        "/btc/exchange-flows/exchange-inflow-total",
        "/btc/exchange-flows/inflow-total",
    ],
    "mvrv": [
        "/btc/market-indicator/mvrv",
        "/btc/market-indicator/mvrv-ratio",
        "/btc/market-data/mvrv",
    ],
    "sopr": [
        "/btc/network-indicator/sopr",
        "/btc/market-indicator/sopr",
    ],
    "eth_netflow": [
        "/eth/exchange-flows/netflow",
        "/eth/exchange-flows/exchange-netflow-total",
    ],
}

# Cache 5 minuti — on-chain data non cambia cosi' spesso
CACHE_TTL = 300


@dataclass
class OnChainData:
    """Dati on-chain aggregati per un asset."""
    asset: str = "btc"

    # Exchange flows (ultimi dati)
    netflow: float = 0.0        # Positivo = depositi (bearish), Negativo = prelievi (bullish)
    inflow: float = 0.0         # BTC depositati su exchange
    outflow: float = 0.0        # BTC prelevati da exchange
    netflow_ma7: float = 0.0    # Media mobile 7 periodi del netflow

    # Market indicators
    mvrv: float = 1.0           # Market Value / Realized Value
    sopr: float = 1.0           # Spent Output Profit Ratio

    fetched_at: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.fetched_at) < CACHE_TTL if self.fetched_at > 0 else False

    @property
    def flow_signal(self) -> str:
        """
        Segnale basato su exchange netflow.
        Grandi outflow = bullish (accumulazione whale).
        Grandi inflow = bearish (distribuzione).
        """
        if not self.is_fresh:
            return "UNKNOWN"

        # Netflow normalizzato: usiamo il netflow rispetto alla media
        # Se non abbiamo la media, usiamo solo il segno
        if self.netflow_ma7 != 0:
            ratio = self.netflow / abs(self.netflow_ma7) if self.netflow_ma7 != 0 else 0
            if ratio < -1.5:
                return "STRONG_OUTFLOW"     # Grande accumulazione → bullish
            elif ratio < -0.5:
                return "MILD_OUTFLOW"       # Moderata accumulazione
            elif ratio > 1.5:
                return "STRONG_INFLOW"      # Grande distribuzione → bearish
            elif ratio > 0.5:
                return "MILD_INFLOW"        # Moderata distribuzione
        else:
            if self.netflow < -500:
                return "STRONG_OUTFLOW"
            elif self.netflow < -100:
                return "MILD_OUTFLOW"
            elif self.netflow > 500:
                return "STRONG_INFLOW"
            elif self.netflow > 100:
                return "MILD_INFLOW"

        return "NEUTRAL"

    @property
    def flow_direction(self) -> float:
        """
        Segnale numerico: -1.0 (forte outflow/bullish) a +1.0 (forte inflow/bearish).
        """
        if not self.is_fresh:
            return 0.0

        signal = self.flow_signal
        return {
            "STRONG_OUTFLOW": -1.0,
            "MILD_OUTFLOW": -0.5,
            "NEUTRAL": 0.0,
            "MILD_INFLOW": 0.5,
            "STRONG_INFLOW": 1.0,
            "UNKNOWN": 0.0,
        }.get(signal, 0.0)

    @property
    def mvrv_signal(self) -> str:
        """
        Segnale basato su MVRV.
        MVRV alto = mercato sopravvalutato (potenziale top).
        MVRV basso = mercato sottovalutato (potenziale bottom).
        """
        if not self.is_fresh or self.mvrv == 0:
            return "UNKNOWN"

        if self.mvrv > 3.7:
            return "EXTREME_HIGH"   # Storico: top di mercato
        elif self.mvrv > 2.5:
            return "HIGH"           # Sopravvalutato
        elif self.mvrv > 1.5:
            return "ELEVATED"       # Moderatamente sopra
        elif self.mvrv > 1.0:
            return "NORMAL"         # Fair value
        elif self.mvrv > 0.8:
            return "LOW"            # Sottovalutato
        else:
            return "EXTREME_LOW"    # Storico: bottom di mercato

    @property
    def mvrv_bias(self) -> float:
        """
        Bias MVRV: -1.0 (molto sottovalutato → bullish) a +1.0 (molto sopravvalutato → bearish).
        Usato per aggiustare la probabilita' nei mercati crypto.
        """
        if not self.is_fresh or self.mvrv == 0:
            return 0.0

        # Normalizza MVRV: 1.0 = neutro, >1 = bearish bias, <1 = bullish bias
        # Range tipico: 0.5 a 4.0
        if self.mvrv > 1.0:
            # Sopra fair value: bearish bias crescente
            return min((self.mvrv - 1.0) / 3.0, 1.0)
        else:
            # Sotto fair value: bullish bias crescente
            return max(-(1.0 - self.mvrv) / 0.5, -1.0)


@dataclass
class CryptoQuantFeed:
    """
    Client per CryptoQuant API v1.

    Fetcha dati on-chain per BTC e ETH.
    Auto-discovery degli endpoint (prova varianti di naming).
    Degrada gracefully se API key manca o API irraggiungibile.
    """
    _api_key: str = ""
    _cache: dict[str, OnChainData] = field(default_factory=dict)
    _session: requests.Session | None = None
    _available: bool | None = None
    _discovered_endpoints: dict[str, str] = field(default_factory=dict)
    _last_error: str = ""

    def __post_init__(self):
        self._api_key = os.environ.get("CRYPTOQUANT_API_KEY", "").strip()
        if not self._api_key:
            logger.info("[CQUANT] Nessuna API key — on-chain data disabilitato")
            self._available = False
            return

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": "PolymarketBot/4.0",
        })

        # Flag per tentare formati auth alternativi se Bearer fallisce
        self._auth_format = "bearer"  # "bearer", "query_param", "x_api_key"
        self._auth_attempts = 0

        self._cache = {
            "btc": OnChainData(asset="btc"),
            "eth": OnChainData(asset="eth"),
        }

        logger.info("[CQUANT] Feed inizializzato — BTC + ETH on-chain data")

    # ── Accesso dati ─────────────────────────────────────────────

    def get_onchain(self, asset: str = "btc") -> OnChainData:
        """
        Ottieni dati on-chain per un asset.
        Se in cache e freschi, non chiama l'API.
        """
        asset = asset.lower()
        if asset not in ("btc", "eth"):
            return OnChainData(asset=asset)

        cached = self._cache.get(asset)
        if cached and cached.is_fresh:
            return cached

        self._fetch_all(asset)
        return self._cache.get(asset, OnChainData(asset=asset))

    def onchain_summary(self) -> str:
        """Stringa riassuntiva per il log."""
        btc = self._cache.get("btc")
        if btc and btc.is_fresh:
            return (
                f"BTC: Netflow={btc.netflow:+.1f} ({btc.flow_signal}) "
                f"MVRV={btc.mvrv:.2f} ({btc.mvrv_signal}) "
                f"SOPR={btc.sopr:.3f}"
            )
        return "On-chain: --"

    # ── Fetch aggregato ──────────────────────────────────────────

    def _fetch_all(self, asset: str = "btc"):
        """Fetcha tutti i dati on-chain per un asset."""
        if self._available is False:
            return

        data = self._cache.get(asset, OnChainData(asset=asset))

        # 1. Exchange Netflow
        netflow_key = "netflow" if asset == "btc" else "eth_netflow"
        netflow_data = self._fetch_metric(netflow_key, window="hour", limit=24)
        if netflow_data:
            latest = netflow_data[-1]
            data.netflow = self._extract_value(latest)
            data.outflow = abs(data.netflow) if data.netflow < 0 else 0
            data.inflow = data.netflow if data.netflow > 0 else 0

            # Calcola MA7 se abbiamo abbastanza dati
            if len(netflow_data) >= 7:
                recent_7 = [self._extract_value(d) for d in netflow_data[-7:]]
                data.netflow_ma7 = sum(recent_7) / len(recent_7)

        # 2. Inflow separato (se disponibile)
        if asset == "btc":
            inflow_data = self._fetch_metric("inflow", window="hour", limit=1)
            if inflow_data:
                data.inflow = self._extract_value(inflow_data[-1])

        # 3. MVRV
        if asset == "btc":
            mvrv_data = self._fetch_metric("mvrv", window="day", limit=1)
            if mvrv_data:
                data.mvrv = self._extract_value(mvrv_data[-1])

        # 4. SOPR
        if asset == "btc":
            sopr_data = self._fetch_metric("sopr", window="day", limit=1)
            if sopr_data:
                data.sopr = self._extract_value(sopr_data[-1])

        data.fetched_at = time.time()
        self._cache[asset] = data

        if data.netflow != 0 or data.mvrv != 1.0:
            logger.debug(
                f"[CQUANT] {asset.upper()}: "
                f"Netflow={data.netflow:+.1f} ({data.flow_signal}) "
                f"MVRV={data.mvrv:.2f} SOPR={data.sopr:.3f}"
            )

    # ── Fetch singolo metric ─────────────────────────────────────

    def _fetch_metric(
        self, metric_key: str, window: str = "day", limit: int = 1
    ) -> list[dict] | None:
        """
        Fetcha un singolo metric dall'API.
        Usa auto-discovery: prova varianti di endpoint fino a trovarne uno che funziona.
        """
        if self._available is False or not self._session:
            return None

        # Se abbiamo gia' scoperto l'endpoint, usalo
        if metric_key in self._discovered_endpoints:
            endpoint = self._discovered_endpoints[metric_key]
            return self._do_fetch(endpoint, window, limit)

        # Auto-discovery: prova ogni variante
        variants = ENDPOINT_VARIANTS.get(metric_key, [])
        for endpoint in variants:
            result = self._do_fetch(endpoint, window, limit)
            if result is not None:
                self._discovered_endpoints[metric_key] = endpoint
                if self._available is None:
                    self._available = True
                    logger.info(
                        f"[CQUANT] API connessa — "
                        f"primo endpoint funzionante: {endpoint}"
                    )
                return result

        return None

    def _do_fetch(
        self, endpoint: str, window: str, limit: int
    ) -> list[dict] | None:
        """Esegui la chiamata HTTP effettiva."""
        if not self._session or self._available is False:
            return None

        # Calcola date
        now = datetime.utcnow()
        from_date = (now - timedelta(days=7)).strftime("%Y%m%d")

        url = f"{BASE_URL}{endpoint}"
        params = {
            "window": window,
            "from": from_date,
            "limit": limit,
        }
        # "exchange" solo per endpoint exchange-flows (non per MVRV, SOPR)
        if "exchange-flow" in endpoint:
            params["exchange"] = "all_exchange"

        # Se stiamo usando auth via query param, aggiungi la key ai params
        if self._auth_format == "query_param":
            params["api_key"] = self._api_key

        try:
            resp = self._session.get(url, params=params, timeout=10)

            if resp.status_code == 200:
                body = resp.json()
                # CryptoQuant response: {"status": {...}, "result": {"data": [...]}}
                return self._extract_data_list(body)

            elif resp.status_code == 401:
                self._auth_attempts += 1
                # Prova formati auth alternativi prima di arrendersi
                if self._auth_format == "bearer" and self._auth_attempts <= 3:
                    logger.info("[CQUANT] Bearer auth fallita, provo X-API-KEY...")
                    self._auth_format = "x_api_key"
                    self._session.headers.pop("Authorization", None)
                    self._session.headers["X-API-KEY"] = self._api_key
                    return None  # Riprova al prossimo ciclo con nuovo formato
                elif self._auth_format == "x_api_key" and self._auth_attempts <= 6:
                    logger.info("[CQUANT] X-API-KEY fallita, provo query param...")
                    self._auth_format = "query_param"
                    self._session.headers.pop("X-API-KEY", None)
                    return None  # Riprova al prossimo ciclo
                else:
                    logger.warning(
                        f"[CQUANT] API key non valida con tutti i formati auth "
                        f"(Bearer, X-API-KEY, query param). "
                        f"Verifica che il piano CryptoQuant includa accesso API."
                    )
                    self._available = False
                    return None

            elif resp.status_code == 429:
                logger.warning("[CQUANT] Rate limit raggiunto (429)")
                return None

            elif resp.status_code == 403:
                # Endpoint richiede piano superiore — non disabilitare tutto
                logger.debug(f"[CQUANT] 403 su {endpoint} — piano insufficiente?")
                return None

            else:
                logger.debug(f"[CQUANT] HTTP {resp.status_code} su {endpoint}")
                return None

        except requests.Timeout:
            logger.debug(f"[CQUANT] Timeout su {endpoint}")
            return None
        except requests.RequestException as e:
            logger.debug(f"[CQUANT] Errore: {e}")
            return None

    # ── Parsing ──────────────────────────────────────────────────

    @staticmethod
    def _extract_data_list(body: dict) -> list[dict] | None:
        """
        Estrai la lista di datapoint dalla risposta CryptoQuant.

        Formati possibili:
        - {"result": {"data": [...]}}
        - {"data": [...]}
        - {"result": [...]}
        - [...]  (direttamente)
        """
        if isinstance(body, list):
            return body if body else None

        # Formato standard: result.data
        if "result" in body:
            result = body["result"]
            if isinstance(result, dict) and "data" in result:
                data = result["data"]
                if isinstance(data, list) and data:
                    return data
            elif isinstance(result, list) and result:
                return result

        # Formato alternativo: data direttamente
        if "data" in body:
            data = body["data"]
            if isinstance(data, list) and data:
                return data

        return None

    @staticmethod
    def _extract_value(datapoint: dict) -> float:
        """
        Estrai il valore numerico da un datapoint CryptoQuant.

        I campi possono essere: "value", "netflow", "inflow", "mvrv", "sopr",
        o il primo campo numerico trovato.
        """
        # Campi comuni in ordine di priorita'
        for key in [
            "value", "netflow", "inflow", "outflow",
            "mvrv", "mvrv_ratio", "sopr",
            "exchange_netflow", "exchange_inflow",
            "flow_total", "total",
        ]:
            val = datapoint.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue

        # Fallback: primo valore numerico (escludi timestamp)
        for key, val in datapoint.items():
            if key in ("datetime", "date", "timestamp", "time", "start", "end"):
                continue
            try:
                return float(val)
            except (ValueError, TypeError):
                continue

        return 0.0
