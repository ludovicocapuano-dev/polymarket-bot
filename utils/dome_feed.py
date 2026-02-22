"""
Feed Dome API — Layer unificato per prediction market cross-platform.

Dome (Y Combinator W24) aggrega dati da Polymarket, Kalshi, PredictIt
e altre piattaforme di prediction market in un'unica API REST.

Dome API:
- Base URL: https://api.dome.market (con fallback https://api.domefi.com)
- Auth: header Authorization: Bearer <key> oppure X-API-Key
- Rate limit: 60 req/min (free tier)

Endpoint chiave:
1. GET /v1/markets      — lista mercati cross-platform con prezzi
2. GET /v1/markets/{id} — dettaglio mercato con prezzi per piattaforma
3. GET /v1/arbs         — opportunita' di arbitraggio pre-calcolate
4. GET /v1/platforms     — piattaforme disponibili

Metriche chiave per arbitraggio:
- price_spread: differenza di prezzo tra piattaforme
- roi: rendimento atteso dell'arbitraggio
- matched_markets: mercati matchati cross-platform (stessa domanda)

Uso nel bot:
- arbitrage: sostituisce/complementa ArbBets come fonte di arb cross-platform
- data_driven: confronto prezzi cross-platform come segnale di mispricing
"""

import logging
import os
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

# Endpoint candidati (provati in ordine)
CANDIDATE_BASES = [
    "https://api.dome.market",
    "https://api.domefi.com",
    "https://dome.market/api",
    "https://api.domeapi.com",
]

AUTH_METHODS = ["bearer", "x_api_key", "query_param"]

CACHE_TTL = 120  # 2 minuti (arb sono time-sensitive)


@dataclass
class DomeMarket:
    """Mercato cross-platform da Dome API."""
    dome_id: str = ""             # ID univoco Dome
    question: str = ""            # Domanda del mercato
    category: str = ""            # "crypto" | "politics" | "sports" | etc.

    # Prezzi per piattaforma (0-1)
    polymarket_yes: float = 0.0
    polymarket_no: float = 0.0
    kalshi_yes: float = 0.0
    kalshi_no: float = 0.0
    predictit_yes: float = 0.0
    predictit_no: float = 0.0

    # Metadata
    polymarket_slug: str = ""
    kalshi_slug: str = ""
    volume_total: float = 0.0
    liquidity_total: float = 0.0
    platforms: list[str] = field(default_factory=list)
    updated_at: float = 0.0

    @property
    def has_cross_platform(self) -> bool:
        """True se il mercato e' disponibile su almeno 2 piattaforme."""
        count = 0
        if self.polymarket_yes > 0:
            count += 1
        if self.kalshi_yes > 0:
            count += 1
        if self.predictit_yes > 0:
            count += 1
        return count >= 2

    @property
    def best_arb(self) -> tuple[str, str, float]:
        """
        Trova la migliore opportunita' di arbitraggio tra piattaforme.
        Ritorna: (piattaforma_buy_yes, piattaforma_buy_no, roi).
        """
        prices = {}
        if self.polymarket_yes > 0:
            prices["polymarket"] = (self.polymarket_yes, self.polymarket_no or (1 - self.polymarket_yes))
        if self.kalshi_yes > 0:
            prices["kalshi"] = (self.kalshi_yes, self.kalshi_no or (1 - self.kalshi_yes))
        if self.predictit_yes > 0:
            prices["predictit"] = (self.predictit_yes, self.predictit_no or (1 - self.predictit_yes))

        if len(prices) < 2:
            return ("", "", 0.0)

        # Trova: compra YES su piattaforma con prezzo piu' basso,
        #         compra NO  su piattaforma con prezzo no piu' basso
        best_roi = 0.0
        best_buy_yes = ""
        best_buy_no = ""

        platforms = list(prices.keys())
        for i, p1 in enumerate(platforms):
            for p2 in platforms[i + 1:]:
                yes_p1, no_p1 = prices[p1]
                yes_p2, no_p2 = prices[p2]

                # Scenario A: YES su p1 + NO su p2
                cost_a = yes_p1 + no_p2
                if cost_a < 1.0:
                    roi_a = (1.0 - cost_a) / cost_a
                    if roi_a > best_roi:
                        best_roi = roi_a
                        best_buy_yes = p1
                        best_buy_no = p2

                # Scenario B: YES su p2 + NO su p1
                cost_b = yes_p2 + no_p1
                if cost_b < 1.0:
                    roi_b = (1.0 - cost_b) / cost_b
                    if roi_b > best_roi:
                        best_roi = roi_b
                        best_buy_yes = p2
                        best_buy_no = p1

        return (best_buy_yes, best_buy_no, best_roi)


@dataclass
class DomeArb:
    """Opportunita' di arbitraggio pre-calcolata da Dome."""
    arb_id: str = ""
    market_name: str = ""
    platform_a: str = ""
    platform_b: str = ""
    price_a: float = 0.0      # Prezzo YES su platform A
    price_b: float = 0.0      # Prezzo YES su platform B
    roi: float = 0.0           # ROI dell'arbitraggio
    total_cost: float = 0.0    # Costo totale (< 1.0 = profittabile)
    category: str = ""
    polymarket_slug: str = ""
    polymarket_token_id: str = ""
    updated_at: float = 0.0
    raw_data: dict = field(default_factory=dict)


@dataclass
class DomeFeed:
    """
    Client per Dome API — Layer unificato prediction market.

    Fetcha dati cross-platform per arbitraggio e analisi.
    Discovery automatica dell'endpoint + auth.
    Degrada gracefully se API irraggiungibile.
    """
    _api_key: str = ""
    _session: requests.Session = field(default_factory=requests.Session)

    # Discovery
    _discovered_base: str = ""
    _discovered_auth: str = ""
    _discovery_done: bool = False
    _discovery_failed: bool = False

    # Cache
    _arb_cache: list[DomeArb] = field(default_factory=list)
    _market_cache: list[DomeMarket] = field(default_factory=list)
    _cache_time: float = 0.0

    def __post_init__(self):
        self._api_key = self._api_key or os.getenv("DOME_API_KEY", "")
        if self._api_key:
            logger.info("[DOME] API key configurata — discovery al primo fetch")
        else:
            logger.info("[DOME] Nessuna API key — provider disabilitato")

    @property
    def available(self) -> bool:
        return bool(self._api_key) and not self._discovery_failed

    # ── Accesso dati ─────────────────────────────────────────

    def get_cross_platform_arbs(self) -> list[DomeArb]:
        """
        Ottieni opportunita' di arbitraggio cross-platform.
        Prima prova l'endpoint /arbs dedicato, poi calcola dai mercati.
        """
        if not self._api_key:
            return []

        now = time.time()
        if self._arb_cache and now - self._cache_time < CACHE_TTL:
            return self._arb_cache

        if not self._discovery_done:
            self._run_discovery()

        if self._discovery_failed:
            return []

        # Prova endpoint arbs dedicato
        arbs = self._fetch_arbs()
        if arbs:
            self._arb_cache = arbs
            self._cache_time = now
            logger.info(f"[DOME] {len(arbs)} arb cross-platform (via /arbs)")
            return self._arb_cache

        # Fallback: calcola arbs dai mercati
        markets = self._fetch_markets()
        if markets:
            self._market_cache = markets
            computed_arbs = self._compute_arbs_from_markets(markets)
            if computed_arbs:
                self._arb_cache = computed_arbs
                self._cache_time = now
                logger.info(
                    f"[DOME] {len(computed_arbs)} arb calcolati da "
                    f"{len(markets)} mercati cross-platform"
                )

        return self._arb_cache

    def get_polymarket_arbs(self) -> list[DomeArb]:
        """Filtra solo arb che coinvolgono Polymarket."""
        all_arbs = self.get_cross_platform_arbs()
        return [
            a for a in all_arbs
            if "polymarket" in a.platform_a.lower()
            or "polymarket" in a.platform_b.lower()
        ]

    def get_market_prices(self, slug: str = "") -> DomeMarket | None:
        """
        Ottieni prezzi cross-platform per un mercato specifico.
        Utile per data_driven: confronta il prezzo Polymarket con Kalshi.
        """
        if not self._market_cache:
            self._market_cache = self._fetch_markets() or []

        if slug:
            for m in self._market_cache:
                if slug.lower() in m.polymarket_slug.lower() or \
                   slug.lower() in m.question.lower():
                    return m
        return None

    def status_summary(self) -> str:
        if not self._api_key:
            return "Dome: no API key"
        if self._discovery_failed:
            return "Dome: discovery failed"
        if not self._discovery_done:
            return "Dome: pending discovery"
        n = len(self._arb_cache)
        return f"Dome: {n} arbs ({self._discovered_base})"

    # ── Discovery ─────────────────────────────────────────────

    def _run_discovery(self):
        """Prova tutte le combinazioni base+auth fino al primo successo."""
        logger.info("[DOME] Avvio discovery endpoint...")

        for base in CANDIDATE_BASES:
            for auth in AUTH_METHODS:
                headers = self._build_headers(auth)
                params = self._build_params(auth)

                # Prova /v1/markets come health check
                for path in ["/v1/markets", "/v1/arbs", "/markets", "/arbs"]:
                    url = base + path
                    try:
                        resp = self._session.get(
                            url, headers=headers, params=params, timeout=10
                        )

                        if resp.status_code == 200:
                            data = resp.json()
                            if self._looks_like_market_data(data):
                                self._discovered_base = base
                                self._discovered_auth = auth
                                self._discovery_done = True
                                logger.info(f"[DOME] Discovery OK: {url} (auth={auth})")
                                return

                        elif resp.status_code == 401:
                            continue  # Auth sbagliata
                        elif resp.status_code == 404:
                            break  # Path non esiste, prova prossimo base

                    except requests.RequestException as e:
                        logger.debug(f"[DOME] Errore connessione {url}: {e}")
                        break  # Host non raggiungibile
                    except ValueError:
                        continue

        self._discovery_done = True
        self._discovery_failed = True
        logger.warning(
            "[DOME] Discovery fallita — nessun endpoint funzionante. "
            "Verifica la API key e il piano. "
            "L'arbitraggio interno e ArbBets continuano a funzionare."
        )

    def _build_headers(self, auth: str) -> dict:
        headers = {"Accept": "application/json"}
        if auth == "bearer":
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif auth == "x_api_key":
            headers["X-API-Key"] = self._api_key
        return headers

    def _build_params(self, auth: str) -> dict:
        if auth == "query_param":
            return {"api_key": self._api_key}
        return {}

    def _looks_like_market_data(self, data) -> bool:
        """Verifica se la risposta contiene dati di mercato/arb."""
        if isinstance(data, list) and len(data) > 0:
            first = data[0]
            if isinstance(first, dict):
                market_keys = {
                    "market", "question", "title", "price", "platform",
                    "roi", "arb", "spread", "polymarket", "kalshi",
                }
                return bool(market_keys & {k.lower() for k in first.keys()})

        if isinstance(data, dict):
            for key in ["data", "markets", "opportunities", "arbs", "results"]:
                if key in data and isinstance(data[key], list):
                    return True
            # Potrebbe essere un singolo mercato
            if "price" in data or "platforms" in data:
                return True

        return False

    # ── Fetch ─────────────────────────────────────────────────

    def _do_get(self, path: str, params: dict | None = None) -> dict | list | None:
        """Esegui GET su un endpoint scoperto."""
        if not self._discovered_base or self._discovery_failed:
            return None

        url = self._discovered_base + path
        headers = self._build_headers(self._discovered_auth)
        all_params = self._build_params(self._discovered_auth)
        if params:
            all_params.update(params)

        try:
            resp = self._session.get(url, headers=headers, params=all_params, timeout=12)

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 401:
                logger.warning("[DOME] Auth fallita — key scaduta o revocata")
                self._discovery_failed = True
            elif resp.status_code == 429:
                logger.warning("[DOME] Rate limit (429)")
            else:
                logger.debug(f"[DOME] HTTP {resp.status_code} su {path}")

        except requests.Timeout:
            logger.debug(f"[DOME] Timeout su {path}")
        except requests.RequestException as e:
            logger.debug(f"[DOME] Errore: {e}")

        return None

    def _fetch_arbs(self) -> list[DomeArb]:
        """Fetch opportunita' arb dall'endpoint dedicato."""
        for path in ["/v1/arbs", "/arbs", "/v1/opportunities"]:
            data = self._do_get(path, {"platform": "polymarket"})
            if data:
                return self._parse_arbs(data)
        return []

    def _fetch_markets(self) -> list[DomeMarket]:
        """Fetch mercati cross-platform."""
        for path in ["/v1/markets", "/markets"]:
            data = self._do_get(path, {"cross_platform": "true", "limit": "200"})
            if data:
                return self._parse_markets(data)
        return []

    # ── Parsing ───────────────────────────────────────────────

    def _parse_arbs(self, data) -> list[DomeArb]:
        """Parse risposta /arbs in DomeArb."""
        raw_list = self._extract_list(data)
        arbs = []

        for i, item in enumerate(raw_list):
            if not isinstance(item, dict):
                continue
            try:
                arb = self._parse_single_arb(item, i)
                if arb and arb.roi > 0.005:  # Min 0.5% ROI
                    arbs.append(arb)
            except Exception as e:
                logger.debug(f"[DOME] Errore parsing arb {i}: {e}")

        return arbs

    def _parse_single_arb(self, item: dict, idx: int) -> DomeArb | None:
        arb_id = str(item.get("id", item.get("arb_id", f"dome_{idx}")))
        market_name = str(item.get("market", item.get("question",
                          item.get("title", item.get("market_name", "Unknown")))))

        platform_a = self._extract_platform_name(item, "a")
        platform_b = self._extract_platform_name(item, "b")
        price_a = self._extract_price_val(item, "a")
        price_b = self._extract_price_val(item, "b")

        roi = self._safe_float(item, "roi",
              self._safe_float(item, "edge",
              self._safe_float(item, "spread",
              self._safe_float(item, "profit", 0.0))))

        total_cost = self._safe_float(item, "total_cost",
                     self._safe_float(item, "cost", 0.0))

        if roi == 0 and total_cost > 0 and total_cost < 1.0:
            roi = (1.0 - total_cost) / total_cost

        if roi <= 0 and price_a == 0 and price_b == 0:
            return None

        category = str(item.get("category", item.get("type", "other"))).lower()
        poly_slug = str(item.get("polymarket_slug", item.get("slug", "")))
        poly_token = str(item.get("polymarket_token_id", item.get("token_id", "")))

        return DomeArb(
            arb_id=arb_id,
            market_name=market_name,
            platform_a=platform_a,
            platform_b=platform_b,
            price_a=price_a,
            price_b=price_b,
            roi=roi if roi < 1.0 else roi / 100.0,  # Normalizza percentuali
            total_cost=total_cost,
            category=category,
            polymarket_slug=poly_slug,
            polymarket_token_id=poly_token,
            updated_at=time.time(),
            raw_data=item,
        )

    def _parse_markets(self, data) -> list[DomeMarket]:
        """Parse risposta /markets in DomeMarket."""
        raw_list = self._extract_list(data)
        markets = []

        for item in raw_list:
            if not isinstance(item, dict):
                continue
            try:
                m = self._parse_single_market(item)
                if m:
                    markets.append(m)
            except Exception as e:
                logger.debug(f"[DOME] Errore parsing market: {e}")

        return markets

    def _parse_single_market(self, item: dict) -> DomeMarket | None:
        """Parse un singolo mercato con prezzi per piattaforma."""
        m = DomeMarket()
        m.dome_id = str(item.get("id", item.get("dome_id", "")))
        m.question = str(item.get("question", item.get("title",
                         item.get("market", ""))))
        m.category = str(item.get("category", item.get("type", ""))).lower()

        # Prezzi — diversi formati possibili
        # Formato 1: platforms nested
        platforms = item.get("platforms", item.get("prices", {}))
        if isinstance(platforms, dict):
            poly = platforms.get("polymarket", platforms.get("poly", {}))
            if isinstance(poly, dict):
                m.polymarket_yes = self._to_prob(poly.get("yes", poly.get("price", 0)))
                m.polymarket_no = self._to_prob(poly.get("no", 0))
                m.polymarket_slug = str(poly.get("slug", poly.get("id", "")))

            kalshi = platforms.get("kalshi", {})
            if isinstance(kalshi, dict):
                m.kalshi_yes = self._to_prob(kalshi.get("yes", kalshi.get("price", 0)))
                m.kalshi_no = self._to_prob(kalshi.get("no", 0))
                m.kalshi_slug = str(kalshi.get("slug", kalshi.get("id", "")))

            predictit = platforms.get("predictit", platforms.get("predict_it", {}))
            if isinstance(predictit, dict):
                m.predictit_yes = self._to_prob(predictit.get("yes", predictit.get("price", 0)))
                m.predictit_no = self._to_prob(predictit.get("no", 0))

        # Formato 2: campi piatti
        if m.polymarket_yes == 0:
            m.polymarket_yes = self._to_prob(item.get("polymarket_yes",
                               item.get("polymarket_price", 0)))
            m.polymarket_no = self._to_prob(item.get("polymarket_no", 0))
        if m.kalshi_yes == 0:
            m.kalshi_yes = self._to_prob(item.get("kalshi_yes",
                           item.get("kalshi_price", 0)))
            m.kalshi_no = self._to_prob(item.get("kalshi_no", 0))

        m.volume_total = self._safe_float(item, "volume",
                         self._safe_float(item, "total_volume", 0.0))
        m.liquidity_total = self._safe_float(item, "liquidity",
                            self._safe_float(item, "total_liquidity", 0.0))

        platform_list = item.get("available_platforms", item.get("platforms_list", []))
        if isinstance(platform_list, list):
            m.platforms = [str(p).lower() for p in platform_list]

        m.updated_at = time.time()

        if not m.question:
            return None
        return m

    def _compute_arbs_from_markets(self, markets: list[DomeMarket]) -> list[DomeArb]:
        """Calcola arb dai dati di mercato cross-platform."""
        arbs = []

        for m in markets:
            if not m.has_cross_platform:
                continue

            buy_yes_plat, buy_no_plat, roi = m.best_arb
            if roi < 0.005:  # Min 0.5%
                continue

            # Determina prezzi
            prices = {
                "polymarket": m.polymarket_yes,
                "kalshi": m.kalshi_yes,
                "predictit": m.predictit_yes,
            }
            price_a = prices.get(buy_yes_plat, 0)
            price_b = prices.get(buy_no_plat, 0)

            arb = DomeArb(
                arb_id=f"dome_calc_{m.dome_id}",
                market_name=m.question,
                platform_a=buy_yes_plat,
                platform_b=buy_no_plat,
                price_a=price_a,
                price_b=price_b,
                roi=roi,
                total_cost=price_a + (1 - price_b) if price_b > 0 else 0,
                category=m.category,
                polymarket_slug=m.polymarket_slug,
                updated_at=time.time(),
            )
            arbs.append(arb)

        arbs.sort(key=lambda a: a.roi, reverse=True)
        return arbs

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _extract_list(data) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ["data", "markets", "opportunities", "arbs", "results", "items"]:
                val = data.get(key)
                if isinstance(val, list):
                    return val
        return []

    def _extract_platform_name(self, item: dict, side: str) -> str:
        direct = item.get(f"platform_{side}", item.get(f"platform{side.upper()}", ""))
        if direct:
            return str(direct).lower()
        platforms = item.get("platforms", [])
        if isinstance(platforms, list):
            idx = 0 if side == "a" else 1
            if idx < len(platforms):
                return str(platforms[idx]).lower()
        if "polymarket_price" in item or "polymarket_yes" in item:
            return "polymarket" if side == "a" else "kalshi"
        return "unknown"

    def _extract_price_val(self, item: dict, side: str) -> float:
        for key in [f"price_{side}", f"price{side.upper()}", f"odds_{side}",
                    f"yes_{side}", f"probability_{side}"]:
            val = item.get(key)
            if val is not None:
                return self._to_prob(val)
        if side == "a":
            for key in ["polymarket_price", "polymarket_yes", "poly_price"]:
                val = item.get(key)
                if val is not None:
                    return self._to_prob(val)
        else:
            for key in ["kalshi_price", "kalshi_yes", "other_price"]:
                val = item.get(key)
                if val is not None:
                    return self._to_prob(val)
        return 0.0

    @staticmethod
    def _to_prob(val) -> float:
        try:
            f = float(val)
            if f > 1.0:
                f = f / 100.0
            return max(0.0, min(1.0, f))
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _safe_float(d: dict, key: str, default: float = 0.0) -> float:
        val = d.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default
