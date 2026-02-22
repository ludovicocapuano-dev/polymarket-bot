"""
Client ArbBets API — Arbitraggio cross-platform (Polymarket vs Kalshi vs Opinion)

ArbBets (getarbitragebets.com) trova 80-100 opportunita' di arbitraggio
giornaliere tra prediction market con ROI medio del 4.87%.

Poiche' la documentazione API non e' pubblica, questo client:
1. Prova diversi endpoint e metodi di autenticazione
2. Si auto-configura al primo successo (discovery)
3. Mantiene cache con refresh ogni 2 minuti (arb sono time-sensitive)
4. Degrada silenziosamente se l'API non risponde

Autenticazione nota: API key con prefisso "ak_"
"""

import logging
import os
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

# Endpoint candidati (provati in ordine fino al primo successo)
CANDIDATE_ENDPOINTS = [
    # Formato 1: /api/v1/ con path
    {"base": "https://getarbitragebets.com/api/v1", "arb": "/arbitrage", "ev": "/ev"},
    # Formato 2: /api/ senza versione
    {"base": "https://getarbitragebets.com/api", "arb": "/arbitrage", "ev": "/ev"},
    # Formato 3: api subdomain
    {"base": "https://api.getarbitragebets.com/v1", "arb": "/arbitrage", "ev": "/ev"},
    # Formato 4: /api/v2/
    {"base": "https://getarbitragebets.com/api/v2", "arb": "/arbitrage", "ev": "/ev"},
    # Formato 5: /api/v1/ con nomi diversi
    {"base": "https://getarbitragebets.com/api/v1", "arb": "/opportunities", "ev": "/value-bets"},
    # Formato 6: /api/v1/ altro pattern
    {"base": "https://getarbitragebets.com/api/v1", "arb": "/arbs", "ev": "/bets"},
]

# Metodi di autenticazione candidati (provati in ordine)
AUTH_METHODS = [
    "bearer",       # Authorization: Bearer <key>
    "x_api_key",    # X-API-Key: <key>
    "query_param",  # ?api_key=<key>
]

CACHE_DURATION = 120  # 2 minuti (arb sono time-sensitive)


@dataclass
class CrossPlatformArb:
    """Opportunita' di arbitraggio cross-platform da ArbBets."""
    arb_id: str                  # ID univoco dell'opportunita'
    market_name: str             # Nome/domanda del mercato
    platform_a: str              # "polymarket" | "kalshi" | "opinion"
    platform_b: str
    price_a: float               # Prezzo su platform A (0-1)
    price_b: float               # Prezzo su platform B (0-1)
    roi: float                   # ROI% dell'arbitraggio
    total_cost: float            # Costo totale (es. 0.97 = 97 centesimi)
    category: str                # "crypto" | "politics" | "sports" | "weather" | "other"
    polymarket_slug: str         # Slug Polymarket (se disponibile)
    polymarket_token_id: str     # Token ID Polymarket (se disponibile)
    updated_at: float            # Timestamp ultimo aggiornamento
    raw_data: dict = field(default_factory=dict)  # Dati grezzi per debug


@dataclass
class ArbBetsFeed:
    """
    Client API per ArbBets (getarbitragebets.com).

    Cerca automaticamente l'endpoint e il metodo di autenticazione corretti.
    Cache 2 minuti (gli arbitraggi sono molto time-sensitive).
    """

    _api_key: str = ""
    _session: requests.Session = field(default_factory=requests.Session)

    # Stato discovery (viene determinato al primo successo)
    _discovered_endpoint: dict = field(default_factory=dict)
    _discovered_auth: str = ""
    _discovery_done: bool = False
    _discovery_failed: bool = False

    # Cache
    _arb_cache: list[CrossPlatformArb] = field(default_factory=list)
    _ev_cache: list[dict] = field(default_factory=list)
    _cache_time: float = 0.0

    def __post_init__(self):
        self._api_key = self._api_key or os.getenv("ARBBETS_API_KEY", "")
        if self._api_key:
            logger.info("[ARBBETS] API key configurata — discovery al primo fetch")
        else:
            logger.info("[ARBBETS] Nessuna API key — provider disabilitato")

    @property
    def available(self) -> bool:
        return bool(self._api_key) and not self._discovery_failed

    def get_arbitrage_opportunities(self) -> list[CrossPlatformArb]:
        """
        Ottieni opportunita' di arbitraggio cross-platform.

        Al primo call, esegue discovery dell'endpoint.
        Poi usa cache con refresh ogni 2 minuti.
        """
        if not self._api_key:
            return []

        now = time.time()
        if self._arb_cache and now - self._cache_time < CACHE_DURATION:
            return self._arb_cache

        # Discovery se necessario
        if not self._discovery_done:
            self._run_discovery()

        if self._discovery_failed or not self._discovered_endpoint:
            return []

        # Fetch con endpoint e auth scoperti
        arbs = self._fetch_arbs()
        if arbs is not None:
            self._arb_cache = arbs
            self._cache_time = now
            logger.info(f"[ARBBETS] {len(arbs)} arbitraggi cross-platform trovati")
        else:
            logger.debug("[ARBBETS] Fetch arbs fallito, uso cache precedente")

        return self._arb_cache

    def get_polymarket_arbs(self) -> list[CrossPlatformArb]:
        """Filtra solo arbitraggi che coinvolgono Polymarket."""
        all_arbs = self.get_arbitrage_opportunities()
        return [
            a for a in all_arbs
            if "polymarket" in a.platform_a.lower() or "polymarket" in a.platform_b.lower()
        ]

    def status_summary(self) -> str:
        """Stato per il log."""
        if not self._api_key:
            return "ArbBets: no API key"
        if self._discovery_failed:
            return "ArbBets: discovery failed"
        if not self._discovery_done:
            return "ArbBets: pending discovery"
        n = len(self._arb_cache)
        return f"ArbBets: {n} arbs ({self._discovered_endpoint.get('base', '?')})"

    # ── Discovery ─────────────────────────────────────────────

    def _run_discovery(self):
        """Prova tutte le combinazioni endpoint+auth fino al primo successo."""
        logger.info("[ARBBETS] Avvio discovery endpoint...")

        for endpoint in CANDIDATE_ENDPOINTS:
            for auth_method in AUTH_METHODS:
                url = endpoint["base"] + endpoint["arb"]
                headers = self._build_headers(auth_method)
                params = self._build_params(auth_method)

                try:
                    resp = self._session.get(
                        url, headers=headers, params=params, timeout=10
                    )

                    if resp.status_code == 200:
                        # Verifica che il body sia JSON valido
                        data = resp.json()
                        if self._looks_like_arb_data(data):
                            self._discovered_endpoint = endpoint
                            self._discovered_auth = auth_method
                            self._discovery_done = True
                            logger.info(
                                f"[ARBBETS] Discovery OK: {url} "
                                f"(auth={auth_method})"
                            )
                            return

                    elif resp.status_code == 401:
                        # Auth sbagliata, prova altro metodo
                        continue
                    elif resp.status_code == 403:
                        # Potrebbe essere l'endpoint giusto ma auth sbagliata
                        logger.debug(
                            f"[ARBBETS] 403 su {url} con {auth_method}"
                        )
                        continue
                    elif resp.status_code == 404:
                        # Endpoint non esiste, prova il prossimo
                        break  # Skippa altri auth per questo endpoint

                except requests.RequestException as e:
                    logger.debug(f"[ARBBETS] Errore connessione {url}: {e}")
                    break  # Skippa host non raggiungibile
                except ValueError:
                    # JSON non valido
                    continue

        # Se arriviamo qui, nessun endpoint ha funzionato
        self._discovery_done = True
        self._discovery_failed = True
        logger.warning(
            "[ARBBETS] Discovery fallita — nessun endpoint funzionante. "
            "Verifica la API key e il piano (Pro/Premium richiesto). "
            "L'arbitraggio interno Polymarket continua a funzionare."
        )

    def _build_headers(self, auth_method: str) -> dict:
        """Costruisci headers basati sul metodo di autenticazione."""
        headers = {"Accept": "application/json"}
        if auth_method == "bearer":
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif auth_method == "x_api_key":
            headers["X-API-Key"] = self._api_key
        return headers

    def _build_params(self, auth_method: str) -> dict:
        """Costruisci query params basati sul metodo di autenticazione."""
        if auth_method == "query_param":
            return {"api_key": self._api_key}
        return {}

    def _looks_like_arb_data(self, data) -> bool:
        """Verifica se la risposta JSON sembra contenere dati di arbitraggio."""
        if isinstance(data, list) and len(data) > 0:
            # Array di opportunita'
            first = data[0]
            if isinstance(first, dict):
                # Cerca campi tipici di arb data
                arb_keys = {"roi", "edge", "spread", "profit", "arbitrage",
                            "platform", "market", "price", "opportunity"}
                return bool(arb_keys & set(k.lower() for k in first.keys()))

        if isinstance(data, dict):
            # Oggetto con array annidato
            for key in ["data", "opportunities", "arbs", "arbitrage", "results", "bets"]:
                if key in data and isinstance(data[key], list):
                    return True

        return False

    # ── Fetch & Parse ─────────────────────────────────────────

    def _fetch_arbs(self) -> list[CrossPlatformArb] | None:
        """Fetch opportunita' dall'endpoint scoperto."""
        if not self._discovered_endpoint:
            return None

        url = self._discovered_endpoint["base"] + self._discovered_endpoint["arb"]
        headers = self._build_headers(self._discovered_auth)
        params = self._build_params(self._discovered_auth)

        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=15)

            if resp.status_code == 401:
                logger.warning("[ARBBETS] API key scaduta o revocata")
                self._discovery_failed = True
                return None

            if resp.status_code != 200:
                logger.debug(f"[ARBBETS] Fetch {resp.status_code}")
                return None

            data = resp.json()
            return self._parse_arbs(data)

        except Exception as e:
            logger.debug(f"[ARBBETS] Errore fetch: {e}")
            return None

    def _parse_arbs(self, data) -> list[CrossPlatformArb]:
        """
        Parse risposta API in CrossPlatformArb.

        Gestisce diversi formati possibili:
        - Array diretto: [{"market": ..., "roi": ...}, ...]
        - Nested: {"data": [...], "count": 42}
        - Nested alternativo: {"opportunities": [...]}
        """
        raw_list = []

        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            for key in ["data", "opportunities", "arbs", "arbitrage", "results", "bets"]:
                if key in data and isinstance(data[key], list):
                    raw_list = data[key]
                    break

        arbs: list[CrossPlatformArb] = []
        for i, item in enumerate(raw_list):
            if not isinstance(item, dict):
                continue

            try:
                arb = self._parse_single_arb(item, i)
                if arb:
                    arbs.append(arb)
            except Exception as e:
                logger.debug(f"[ARBBETS] Errore parsing item {i}: {e}")

        return arbs

    def _parse_single_arb(self, item: dict, index: int) -> CrossPlatformArb | None:
        """
        Parse un singolo item di arbitraggio.

        Gestisce nomi di campo diversi (snake_case, camelCase, abbreviazioni).
        """
        # ID
        arb_id = str(
            item.get("id", item.get("arb_id", item.get("opportunity_id", f"arb_{index}")))
        )

        # Nome mercato
        market_name = str(
            item.get("market", item.get("market_name", item.get("question",
            item.get("title", item.get("event", "Unknown")))))
        )

        # Piattaforme
        platform_a = self._extract_platform(item, "a")
        platform_b = self._extract_platform(item, "b")

        # Prezzi
        price_a = self._extract_price(item, "a")
        price_b = self._extract_price(item, "b")

        # ROI/Edge
        roi = self._extract_float(
            item, ["roi", "edge", "profit", "spread", "return", "pnl"]
        )

        # Se non troviamo ROI, calcoliamo dal total_cost
        total_cost = self._extract_float(
            item, ["total_cost", "totalCost", "cost", "total"]
        )
        if roi == 0 and total_cost > 0 and total_cost < 1.0:
            roi = (1.0 - total_cost) / total_cost

        # Se non abbiamo dati sufficienti, skip
        if roi <= 0 or (price_a == 0 and price_b == 0):
            return None

        # Categoria
        category = str(
            item.get("category", item.get("type", item.get("market_type", "other")))
        ).lower()

        # Slug e token Polymarket (se disponibili)
        poly_slug = str(item.get("polymarket_slug", item.get("slug", "")))
        poly_token = str(item.get("polymarket_token_id", item.get("token_id", "")))

        return CrossPlatformArb(
            arb_id=arb_id,
            market_name=market_name,
            platform_a=platform_a,
            platform_b=platform_b,
            price_a=price_a,
            price_b=price_b,
            roi=roi,
            total_cost=total_cost if total_cost > 0 else (price_a + price_b),
            category=category,
            polymarket_slug=poly_slug,
            polymarket_token_id=poly_token,
            updated_at=time.time(),
            raw_data=item,
        )

    def _extract_platform(self, item: dict, side: str) -> str:
        """Estrai nome piattaforma da diversi formati."""
        # Formato: platform_a / platform_b
        direct = item.get(f"platform_{side}", item.get(f"platform{side.upper()}", ""))
        if direct:
            return str(direct).lower()

        # Formato: platforms: ["polymarket", "kalshi"]
        platforms = item.get("platforms", item.get("exchanges", []))
        if isinstance(platforms, list):
            idx = 0 if side == "a" else 1
            if idx < len(platforms):
                return str(platforms[idx]).lower()

        # Formato: book_a / book_b
        book = item.get(f"book_{side}", item.get(f"bookmaker_{side}", ""))
        if book:
            return str(book).lower()

        # Formato flat: polymarket_price + kalshi_price → deduce platforms
        if "polymarket_price" in item or "polymarket_yes" in item:
            return "polymarket" if side == "a" else "kalshi"

        return "unknown"

    def _extract_price(self, item: dict, side: str) -> float:
        """Estrai prezzo da diversi formati."""
        # Formato: price_a / price_b
        for key_pattern in [
            f"price_{side}", f"price{side.upper()}",
            f"odds_{side}", f"odds{side.upper()}",
            f"yes_{side}", f"probability_{side}",
        ]:
            val = item.get(key_pattern)
            if val is not None:
                return self._to_probability(val)

        # Formato: polymarket_price / kalshi_price
        if side == "a":
            for key in ["polymarket_price", "polymarket_yes", "poly_price"]:
                val = item.get(key)
                if val is not None:
                    return self._to_probability(val)
        else:
            for key in ["kalshi_price", "kalshi_yes", "kalshi_no", "opinion_price"]:
                val = item.get(key)
                if val is not None:
                    return self._to_probability(val)

        # Formato: prices: [0.65, 0.32]
        prices = item.get("prices", [])
        if isinstance(prices, list):
            idx = 0 if side == "a" else 1
            if idx < len(prices):
                return self._to_probability(prices[idx])

        return 0.0

    @staticmethod
    def _to_probability(val) -> float:
        """Converti un valore a probabilita' 0-1."""
        try:
            f = float(val)
            if f > 1.0:
                f = f / 100.0  # 65 → 0.65
            return max(0.0, min(1.0, f))
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _extract_float(item: dict, keys: list[str]) -> float:
        """Estrai valore float provando diversi nomi di campo."""
        for key in keys:
            val = item.get(key)
            if val is not None:
                try:
                    f = float(val)
                    # Se sembra una percentuale (> 1), converti
                    if f > 1.0 and f < 100:
                        return f / 100.0
                    elif f >= 100:
                        return 0.0  # Probabilmente non e' un ROI
                    return f
                except (ValueError, TypeError):
                    continue
        return 0.0
