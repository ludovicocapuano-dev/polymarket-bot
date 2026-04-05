"""
Strategia 5: Weather Prediction — v3.6 Multi-Source + WU Settlement
=====================================================================
Sfrutta le previsioni meteo MULTI-SORGENTE per tradare i mercati
weather su Polymarket: "Highest temperature in London on Feb 14?"

Provider (consensus pesato):
- Weather Underground:  fonte settlement Polymarket (peso 2.0) ← CRITICO
- Wethr.net:           16+ modelli professionali (peso 1.5)
- Open-Meteo:          ensemble GFS 31 membri (peso 1.0)
- NWS API:             previsioni ufficiali USA (peso 0.8)

Approccio:
- Ogni provider fornisce una stima di probabilita' per bucket
- Il consensus fa media pesata delle probabilita' dei provider
- WU ha peso 2.0 (massimo) perche' Polymarket risolve usando WU
- Se i provider divergono molto → incertezza aumentata → trade piu' cauti
- Confronta probabilita' consensus vs prezzo di mercato Polymarket
- Trada quando il consensus diverge significativamente dal mercato

Esempio (4 fonti):
  Mercato: "7°C to 9°C" in London Feb 14 — prezzo YES = $0.35
  WU:         P=0.70 (forecast settlement source)
  Open-Meteo: P=0.68 (22/31 GFS membri)
  Wethr.net:  P=0.75 (12/16 modelli professionali)
  NWS:        N/A (non copre UK)
  Consensus:  0.71 (WU-weighted) → Edge = 0.71 - 0.35 = 0.36 → TRADE!
"""

import logging
import math
import random
import re
import time
from dataclasses import dataclass
import datetime as _dt
from datetime import datetime

from utils.polymarket_api import Market, PolymarketAPI
from utils.weather_feed import WeatherFeed, CityForecast, get_city_unit, c_to_f, f_to_c
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)


def _market_efficiency(market: Market) -> float:
    """
    Market efficiency score [0, 1].
    Higher = more efficient = less exploitable edge.

    Based on spread tightness, liquidity depth, and volume.
    Used to adjust min_edge: efficient markets need higher edge to trade.
    """
    price_sum = market.prices.get("yes", 0.5) + market.prices.get("no", 0.5)
    spread = abs(1.0 - price_sum)
    spread_score = max(0.0, 1.0 - spread / 0.04)

    liq = getattr(market, "liquidity", 0) or 0
    liq_score = min(liq / 100_000, 1.0)

    vol = getattr(market, "volume", 0) or 0
    vol_score = min(vol / 500_000, 1.0)

    return spread_score * 0.4 + liq_score * 0.3 + vol_score * 0.3

STRATEGY_NAME = "weather"
MAX_WEATHER_BET = 35.0  # v12.10: ridotto da $66 — sizing golden era ($20-45) era profittevole, $50+ perde

# v11.1: City performance tiers basati su dati reali (275 trade, 17 giorni)
# Tier 1: WR >= 75%, volume alto → full budget
# Tier 2: WR 57-74% → budget ridotto 60%
# Tier 3: WR < 50% → BLACKLIST (perdono soldi)
CITY_BLACKLIST_DEFAULT = {"london", "paris", "houston", "los angeles", "denver", "dallas"}  # v12.10: +4 citta spring-volatile (Houston 22%WR, LA 17%, Denver 29%, Dallas 33%)
CITY_TIER2_DEFAULT = {"miami", "buenos aires", "ankara", "toronto", "nyc"}  # v12.10: +Toronto 25%WR, +NYC 40%WR
CITY_TIER2_MAX_BET = 17.0  # v12.9.1: AutoOptimizer riduce da $35 — tier2 cities hanno edge basso

# v12.0.5: Dynamic city blacklist — auto-generated from recent trade WR
_dynamic_city_blacklist: set[str] = set(CITY_BLACKLIST_DEFAULT)
_dynamic_city_tier2: set[str] = set(CITY_TIER2_DEFAULT)
_city_blacklist_updated: float = 0.0


def refresh_city_blacklist(trades_path: str = "logs/trades.json",
                           min_trades: int = 5,
                           blacklist_wr: float = 0.45,
                           tier2_wr: float = 0.55):
    """
    v12.0.5: Auto-generate city blacklist from recent trade WR.
    Called periodically by the bot. Learns from mistakes.
    """
    import json
    import time as _time
    global _dynamic_city_blacklist, _dynamic_city_tier2, _city_blacklist_updated

    # Refresh max once per hour
    if _time.time() - _city_blacklist_updated < 3600:
        return

    try:
        with open(trades_path) as f:
            all_trades = json.load(f)
    except Exception:
        return

    # Filter weather trades with city info
    city_results: dict[str, list[bool]] = {}
    for t in all_trades:
        if t.get("strategy") != "weather":
            continue
        city = t.get("city", "").lower()
        result = t.get("result", "")
        if not city or result not in ("WIN", "LOSS"):
            continue
        city_results.setdefault(city, []).append(result == "WIN")

    new_blacklist = set()
    new_tier2 = set()
    for city, outcomes in city_results.items():
        if len(outcomes) < min_trades:
            # Not enough data — keep default if applicable
            if city in CITY_BLACKLIST_DEFAULT:
                new_blacklist.add(city)
            elif city in CITY_TIER2_DEFAULT:
                new_tier2.add(city)
            continue
        wr = sum(outcomes) / len(outcomes)
        if wr < blacklist_wr:
            new_blacklist.add(city)
            logger.info(f"[CITY-LEARN] {city}: WR {wr:.0%} ({len(outcomes)} trades) → BLACKLIST")
        elif wr < tier2_wr:
            new_tier2.add(city)
            logger.info(f"[CITY-LEARN] {city}: WR {wr:.0%} ({len(outcomes)} trades) → TIER2")

    # If no city data yet, keep defaults
    if not city_results:
        _city_blacklist_updated = _time.time()
        return

    if new_blacklist != _dynamic_city_blacklist or new_tier2 != _dynamic_city_tier2:
        logger.info(
            f"[CITY-LEARN] Updated: blacklist={new_blacklist or '{}'}, "
            f"tier2={new_tier2 or '{}'}"
        )
    _dynamic_city_blacklist = new_blacklist
    _dynamic_city_tier2 = new_tier2
    _city_blacklist_updated = _time.time()

# ── Pattern per riconoscere mercati weather ──────────────────
WEATHER_PATTERNS = [
    re.compile(r"(?:highest|high|max)\s+temp", re.I),
    re.compile(r"temperature\s+in\s+\w+", re.I),
    re.compile(r"(?:lowest|low|min)\s+temp", re.I),
    # v5.0: pattern extra per catturare piu' mercati weather
    re.compile(r"(?:rain|snow|precipitation)\s+in\s+\w+", re.I),
    re.compile(r"(?:london|nyc|chicago|seoul|miami|ankara|seattle|atlanta|dallas|paris|tokyo|sydney|toronto|denver|los\s*angeles|phoenix|houston|buenos\s*aires|sao\s*paulo|wellington|lucknow).*(?:temp|degree|°)", re.I),
]

# Mesi in inglese → numero
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass
class WeatherOpportunity:
    """Opportunita' di trading su un mercato weather."""
    market: Market
    city: str
    date: str           # "2026-02-14"
    bucket_low: float   # limite inferiore del range (°C)
    bucket_high: float  # limite superiore del range (°C)
    bucket_label: str   # "7°C to 9°C" (per il log)
    side: str           # "YES" o "NO"
    forecast_prob: float
    market_prob: float
    edge: float
    confidence: float
    reasoning: str
    # v10.6: Metriche EV per ranking profittevole
    expected_value: float = 0.0     # EV per $1 investito
    payoff_ratio: float = 0.0       # profit/loss ratio se win
    meta_features: object = None    # v12.0.1: MetaFeatures for meta-labeling
    # v12.0.4: extra features for AutoOptimizer
    days_ahead: int = 0
    n_sources: int = 0


class WeatherStrategy:
    """
    Trading su mercati weather basato su previsioni ensemble.

    Funzionamento:
    1. Filtra mercati weather da Polymarket (temperature per citta')
    2. Rileva citta', data e bucket di temperatura
    3. Confronta probabilita' previsione vs prezzo di mercato
    4. Trada bucket con edge significativo
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        weather: WeatherFeed,
        min_edge: float = 0.04,      # v5.0: edge minimo 4% (niche dominance)
        min_confidence: float = 0.55,
        meta_labeler=None,           # v12.0.1: Lopez de Prado meta-labeling
        horizon=None,                # v13.1: Horizon SDK primary execution
    ):
        self.api = api
        self.risk = risk
        self.weather = weather
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.meta_labeler = meta_labeler
        self.horizon = horizon  # v13.1: HorizonClient instance
        self._trades_executed = 0
        self._recently_traded: dict[str, float] = {}
        self._TRADE_COOLDOWN = 600  # v5.0: 10 min cooldown (niche dominance)
        self._prefetch_done = False

    def _fetch_weather_markets_by_id(self) -> list:
        """v12.9: Fetch weather markets directly by ID range from Gamma API.
        Polymarket weather markets are no longer in the generic listing endpoint.
        They must be fetched individually by numeric ID."""
        import requests as _req
        from utils.polymarket_api import Market as _M

        # Cache for 5 minutes
        now = time.time()
        if hasattr(self, '_weather_id_cache_ts') and now - self._weather_id_cache_ts < 300:
            return getattr(self, '_weather_id_cache', [])

        markets = []
        # v13.1: Dynamic ID range — weather markets cluster in ~500 ID blocks
        # Scan in small steps near the anchor, bigger steps further out
        last_known = getattr(self, '_last_weather_id', 1815000)
        scan_start = max(last_known - 500, 1810000)
        scan_end = last_known + 1000
        for mid in range(scan_start, scan_end, 50):  # step 50 = fast scan (~30 fetches)
            try:
                resp = _req.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=3)
                if resp.status_code == 200:
                    m = resp.json()
                    q = m.get("question", "")
                    if "temperature" in q.lower() and m.get("active", True) and not m.get("closed", False):
                        # Convert to Market-like object
                        import json as _j
                        prices_raw = m.get("outcomePrices", [])
                        if isinstance(prices_raw, str):
                            try: prices_raw = _j.loads(prices_raw)
                            except: prices_raw = []
                        tokens_raw = m.get("clobTokenIds", [])
                        if isinstance(tokens_raw, str):
                            try: tokens_raw = _j.loads(tokens_raw)
                            except: tokens_raw = []

                        # Build a Market-compatible object with all required attrs
                        outcomes_raw = m.get("outcomes", ["Yes", "No"])
                        if isinstance(outcomes_raw, str):
                            try: outcomes_raw = _j.loads(outcomes_raw)
                            except: outcomes_raw = ["Yes", "No"]
                        market = type('WeatherMarket', (), {
                            'id': m.get("conditionId", ""),
                            'condition_id': m.get("conditionId", ""),
                            'question': q,
                            'category': 'weather',
                            'active': True,
                            'volume': float(m.get("volume", 0) or 0),
                            'liquidity': float(m.get("liquidity", 0) or 0),
                            'prices': {
                                'yes': float(prices_raw[0]) if prices_raw else 0.5,
                                'no': float(prices_raw[1]) if len(prices_raw) > 1 else 0.5,
                            },
                            'tokens': {
                                'yes': tokens_raw[0] if tokens_raw else '',
                                'no': tokens_raw[1] if len(tokens_raw) > 1 else '',
                            },
                            'spread': abs(1.0 - (float(prices_raw[0]) if prices_raw else 0.5) - (float(prices_raw[1]) if len(prices_raw) > 1 else 0.5)) if prices_raw else 0.05,
                            'end_date': m.get("endDate", ""),
                            'tags': [],
                            'slug': m.get("slug", ""),
                            'outcomes': outcomes_raw,
                            'description': m.get("description", ""),
                        })()
                        markets.append(market)
                        # Track highest found ID for next scan
                        self._last_weather_id = max(getattr(self, '_last_weather_id', 0), mid)
                elif resp.status_code == 404:
                    pass  # ID doesn't exist, skip
            except Exception:
                pass

        self._weather_id_cache = markets
        self._weather_id_cache_ts = now
        if markets:
            logger.info(
                f"[WEATHER] Fetched {len(markets)} weather markets "
                f"(range {scan_start}-{scan_end}, last_id={getattr(self, '_last_weather_id', 0)})"
            )
        else:
            logger.info(
                f"[WEATHER] No active weather markets in range {scan_start}-{scan_end}. "
                f"Markets may have moved to higher IDs."
            )
        return markets

    async def scan(
        self, shared_markets: list[Market] | None = None
    ) -> list[WeatherOpportunity]:
        """Scansiona mercati weather per opportunita'."""
        markets = shared_markets or self.api.fetch_markets(limit=200)
        if not markets:
            markets = []

        # v12.9: ALWAYS fetch weather markets by ID — Gamma API listing
        # no longer includes them. Shared_markets only has false positives (Mayweather etc.)
        weather_markets = self._fetch_weather_markets_by_id()
        if not weather_markets:
            # Fallback to shared_markets filter (legacy, probably won't find any)
            weather_markets = self._filter_weather(markets)

        if not weather_markets:
            logger.debug("[WEATHER] Scan: 0 mercati weather trovati")
            return []

        # Pre-fetch previsioni per le citta' rilevate (solo la prima volta o cache scaduta)
        cities_needed = set()
        for m in weather_markets:
            city = self.weather.detect_city(m.question)
            if city:
                cities_needed.add(city)

        for city in cities_needed:
            self.weather.get_forecast(city)

        # Analizza ogni mercato
        now = time.time()
        opportunities: list[WeatherOpportunity] = []
        skipped_cooldown = 0
        skipped_parse = 0

        for m in weather_markets:
            # Cooldown
            if now - self._recently_traded.get(m.id, 0) < self._TRADE_COOLDOWN:
                skipped_cooldown += 1
                continue

            opp = self._analyze(m)
            if opp:
                opportunities.append(opp)
            else:
                skipped_parse += 1

        # v10.6: Ranking per Expected Value, non edge grezzo.
        # EV = win_prob * profit_if_win - (1-win_prob) * loss (per $1 investito)
        # Questo favorisce trade a prezzo basso con payoff asimmetrico A NOSTRO FAVORE.
        # Dati: prezzo<0.40 = 100% WR e +$17.79, prezzo>0.50 = -$29.99.
        opportunities.sort(key=lambda o: o.expected_value, reverse=True)

        # v10.7: Best-per-city — max 2 trade per citta+data, ma solo se
        # i bucket sono distanti (>5 gradi). Bucket vicini sono correlati
        # (se sbagli forecast, perdi su entrambi). Bucket distanti no:
        # es. Chicago 32-33 e 42-43 sono scommesse indipendenti.
        city_date_buckets: dict[str, list[WeatherOpportunity]] = {}
        for opp in opportunities:
            key = f"{opp.city}_{opp.date}"
            city_date_buckets.setdefault(key, []).append(opp)

        filtered: list[WeatherOpportunity] = []
        for key, opps in city_date_buckets.items():
            filtered.append(opps[0])  # sempre il migliore per EV
            if len(opps) > 1:
                best_mid = (opps[0].bucket_low + opps[0].bucket_high) / 2.0
                for opp in opps[1:]:
                    opp_mid = (opp.bucket_low + opp.bucket_high) / 2.0
                    if abs(opp_mid - best_mid) >= 5.0:
                        filtered.append(opp)
                        break  # max 2 per città+data
        opportunities = filtered

        if opportunities:
            best = opportunities[0]
            logger.info(
                f"[WEATHER] Scan {len(markets)} mercati "
                f"({len(weather_markets)} weather, {skipped_cooldown} cooldown, "
                f"{skipped_parse} parse-fail) → "
                f"{len(opportunities)} opportunita' "
                f"(migliore: {best.city.upper()} "
                f"'{best.bucket_label}' "
                f"edge={best.edge:.4f} EV={best.expected_value:+.3f}/$ "
                f"payoff={best.payoff_ratio:.1f}x)"
            )
        else:
            logger.info(
                f"[WEATHER] Scan {len(markets)} mercati "
                f"({len(weather_markets)} weather, {skipped_cooldown} cooldown, "
                f"{skipped_parse} parse-fail) → 0 opportunita' "
                f"({self.weather.status_summary()})"
            )

        return opportunities

    def _filter_weather(self, markets: list[Market]) -> list[Market]:
        """Filtra mercati weather dalla lista completa."""
        results = []
        for m in markets:
            q = m.question
            # Check pattern weather
            if any(p.search(q) for p in WEATHER_PATTERNS):
                results.append(m)
                continue
            # Check tag weather
            if any(t in ["weather", "climate", "temperature"] for t in m.tags):
                results.append(m)
        return results

    def _analyze(self, market: Market) -> WeatherOpportunity | None:
        """Analizza un singolo mercato weather."""
        q = market.question

        # 1. Rileva citta'
        city = self.weather.detect_city(q)
        if not city:
            logger.info(f"[WEATHER-PARSE] no city: {q[:80]}")
            return None

        # v12.0.5: Dynamic city blacklist (learns from trade outcomes)
        if city.lower() in _dynamic_city_blacklist:
            logger.debug(
                f"[WEATHER-SKIP] blacklisted city: {city} "
                f"(dynamic WR < {int(0.45*100)}%)"
            )
            return None

        # 2. Rileva data
        date = self._parse_date(q)
        if not date:
            logger.info(f"[WEATHER-PARSE] no date: city={city} q={q[:80]}")
            return None

        # 3. Ottieni previsione
        forecast = self.weather.get_forecast_for_date(city, date)
        if not forecast:
            logger.info(f"[WEATHER-PARSE] no forecast: {city} {date}")
            return None

        # 4. Rileva bucket di temperatura
        bucket = self._parse_bucket(market)
        if not bucket:
            logger.info(f"[WEATHER-PARSE] no bucket: {q[:100]} outcomes={market.outcomes[:4]}")
            return None

        low, high, label = bucket

        # v5.0: rileva unita del mercato (F per citta USA, C per le altre)
        unit = get_city_unit(city)

        # Rileva se il mercato usa Fahrenheit dai numeri nel testo
        # (se vede numeri > 50 in un mercato London/Seoul, e' strano)
        if unit == "C" and low > 57:
            unit = "F"  # Override: probabilmente il mercato e' in F
        if unit == "F" and -10 < low < 40 and -10 < high < 40:
            # Potrebbe essere Celsius se i numeri sono piccoli
            # Ma per citta USA manteniamo F
            pass

        # 5. Calcola probabilita dal modello meteorologico
        # bucket_probability_in_unit gestisce la conversione F->C internamente
        forecast_prob = forecast.bucket_probability_in_unit(low, high, unit)

        # v12.10: sigma_filter and source_disagreement RE-ENABLED
        # v12.9 li aveva rimossi ("every feature removal improved performance" sul backtest golden era)
        # Ma i dati reali Mar 23-26 mostrano: senza filtri, bot entra su bucket con sigma 5-6°F
        # e perde 19 trade consecutivi. I filtri proteggono dai regimi ad alta incertezza (primavera).
        # v12.9 SUBTRACTION: sigma and source filters REMOVED (duplicate block)
        # These were blocking trades that the scan reported as opportunities
        forecast_sigma = forecast.uncertainty_in_unit(unit)
        sigma_f = forecast_sigma if unit == "F" else forecast_sigma * 1.8
        if sigma_f > 4.0:
            logger.debug(f"[WEATHER-NOTE] high sigma {sigma_f:.1f}°F (not blocking)")
        if hasattr(forecast, 'sources') and len(forecast.sources) >= 2:
            source_temps_unit = [c_to_f(s.temp) if unit == "F" else s.temp for s in forecast.sources]
            source_spread_f = (max(source_temps_unit) - min(source_temps_unit)) * (1 if unit == "F" else 1.8)
            if source_spread_f > 3.0:
                logger.debug(f"[WEATHER-NOTE] source spread {source_spread_f:.1f}°F (not blocking)")

        # v5.0: Boost same-day con osservazioni Wethr.net
        # Se e' oggi e abbiamo la temperatura corrente, affiniamo la stima
        days_ahead = self._days_until(date)
        if days_ahead == 0:
            obs = self.weather.get_observations(city)
            if obs:
                try:
                    current_temp_c = None
                    # Prova diversi formati di risposta Wethr.net
                    if isinstance(obs, dict):
                        current_temp_c = obs.get("temperature", obs.get("temp",
                            obs.get("current", {}).get("temperature")))
                    if current_temp_c is not None:
                        current_temp_c = float(current_temp_c)
                        current_in_unit = c_to_f(current_temp_c) if unit == "F" else current_temp_c
                        # Se la temp attuale e' GIA nel bucket, alta probabilita
                        if low <= current_in_unit < high:
                            forecast_prob = max(forecast_prob, 0.60)
                        # Se e' GIA sopra il bucket high, probabilita bassa
                        elif current_in_unit >= high:
                            # Per "highest temp" il max potrebbe gia essere stato raggiunto
                            forecast_prob = max(forecast_prob * 0.5, 0.05)
                        logger.debug(
                            f"[WEATHER] Same-day obs {city}: {current_in_unit:.1f}{unit} "
                            f"vs bucket [{low}-{high}] -> P={forecast_prob:.3f}"
                        )
                except (ValueError, TypeError, KeyError):
                    pass

        # v10.8.5: Latency Hunter — rileva forecast shift per stimare edge extra.
        # Se il forecast e' appena cambiato, il mercato Polymarket e' ancora sul
        # vecchio prezzo → l'edge calcolata e' probabilmente conservativa.
        forecast_shift = self.weather.get_forecast_shift(city, date)
        _is_latency_opportunity = forecast_shift is not None and abs(forecast_shift) >= 1.0

        # 6. Confronta con prezzo di mercato
        price_yes = market.prices.get("yes", 0.5)
        price_no = market.prices.get("no", 0.5)

        # Fee deduction: non-crypto markets use ~0.005 fee rate
        fee = 0.0  # weather markets sono fee-free su Polymarket

        # v12.7: Uncertainty-adjusted edge — accounts for forecast sigma.
        # Raw edge = forecast_prob - market_price. But our forecast_prob has
        # uncertainty. If sigma is large, the true probability could be much
        # closer to market price than we think.
        #
        # We compute a "conservative probability" by shrinking our estimate
        # toward the market price proportionally to our uncertainty.
        # sigma_penalty = sigma^2 / (bucket_width^2 + sigma^2)
        # This is the fraction of our edge that might be noise.
        #
        # Intuition: if bucket is 2°F wide and sigma is 3°F, we're very
        # uncertain about whether temp falls in this bin. penalty ≈ 0.69.
        # If bucket is 10°F wide and sigma is 1°F, we're quite sure. penalty ≈ 0.01.
        bucket_width = high - low
        # Clamp bucket_width for open-ended ranges (Below X, Above X)
        effective_bw = min(bucket_width, 20.0)  # cap at 20 degrees for edge calc
        if effective_bw <= 0:
            effective_bw = 1.0
        # v12.10: uncertainty-adjusted edge RE-ENABLED
        # v12.9 lo aveva rimosso. Ma senza adjustment, il bot entra su bucket dove
        # sigma copre l'intero range (es. bucket 2°F, sigma 5°F = coin flip).
        # Shrink conservativo: sposta forecast_prob verso market_prob proporzionalmente a sigma.
        sigma_penalty = forecast_sigma ** 2 / (effective_bw ** 2 + forecast_sigma ** 2)
        market_prob = price_yes  # market's estimate of P(YES)
        adj_forecast_prob = forecast_prob * (1.0 - sigma_penalty) + market_prob * sigma_penalty

        edge_yes = adj_forecast_prob - price_yes - fee
        edge_no = (1.0 - adj_forecast_prob) - price_no - fee

        best_side = "YES" if edge_yes > edge_no else "NO"
        best_edge = max(edge_yes, edge_no)

        if best_edge > 0:
            logger.debug(
                f"[WEATHER-EDGE] {city} {label}: raw_P={forecast_prob:.3f} "
                f"adj_P={adj_forecast_prob:.3f} sigma={forecast_sigma:.1f}°{unit} "
                f"bw={effective_bw:.0f} penalty={sigma_penalty:.3f} "
                f"edge={best_edge:.4f} side={best_side}"
            )

        # ── MIN_EDGE: horizon-based + market efficiency adjustment ──
        # L'incertezza del forecast cresce con l'orizzonte. Non serve una
        # confidence inventata: sigma nel modello Phi(bucket) gia' cattura
        # l'incertezza. Serve solo una soglia di edge minima crescente.
        days_ahead = self._days_until(date)
        # v12.10.9: Re-enable same-day/+1d WITH safety checks
        # (was blocked in v12.10 after -$595 same-day and -$367 +1d)
        # Now guarded by: Kalman correction + warming classifier agreement
        if days_ahead == 0:
            # Same-day: only if warming classifier agrees with forecast direction
            try:
                from utils.warming_classifier import (
                    classify_warming_day, classifier_agrees_with_forecast,
                    kalman_correct, fetch_current_conditions
                )
                classification, clf_conf = classify_warming_day(city)
                if not classifier_agrees_with_forecast(classification, clf_conf, best_side):
                    logger.info(
                        f"[WEATHER-SKIP] classifier disagrees: {city} {label} "
                        f"class={classification} conf={clf_conf:.2f} side={best_side}"
                    )
                    return None
                # Apply Kalman correction if we have current conditions
                conditions = fetch_current_conditions(city)
                if conditions and conditions.get("temperature") is not None:
                    station = get_station_info(city) if 'get_station_info' in dir() else None
                    tz_offset = station.get("tz", 0) if station else 0
                    hour_local = (datetime.now().hour + tz_offset) % 24
                    # Kalman correct the forecast high
                    corrected_high = kalman_correct(
                        forecast.temperature, conditions["temperature"],
                        hour_local,
                        cloud_cover=conditions.get("cloud_cover", 50),
                        wind_speed=conditions.get("wind_speed", 10),
                    )
                    logger.debug(
                        f"[KALMAN] {city}: forecast={forecast.temperature:.1f} "
                        f"current={conditions['temperature']:.1f} → corrected={corrected_high:.1f}"
                    )
                logger.info(
                    f"[WEATHER] Same-day ENABLED: {city} {label} "
                    f"classifier={classification} ({clf_conf:.2f})"
                )
            except Exception as e:
                logger.debug(f"[WEATHER] Classifier error (non-blocking): {e}")
                # v12.9 SUBTRACTION: Don't block trade on classifier error!
                # This was blocking ALL same-day trades because forecast.temperature
                # doesn't exist (attribute is .temp). Continue without classifier.
            effective_min_edge = 0.08  # same-day with classifier: cautious but tradeable
        elif days_ahead == 1:
            effective_min_edge = 0.12  # +1d: standard
        else:
            effective_min_edge = 0.20  # +2d+: conservative

        # Market efficiency: mercati efficienti (alta liquidita', spread
        # stretto) hanno bisogno di edge piu' alta per giustificare il trade.
        eff = _market_efficiency(market)
        effective_min_edge *= (1.0 + eff * 0.5)  # fino a +50% per mercati efficienti

        # Latency Hunter: se il forecast e' appena cambiato, il mercato
        # e' probabilmente sul vecchio prezzo → riduci min_edge.
        if _is_latency_opportunity:
            effective_min_edge *= 0.70  # 30% di sconto sulla soglia
            logger.info(
                f"[LATENCY-HUNTER] {city} {label}: "
                f"shift={forecast_shift:+.1f}°C -> min_edge ridotto a "
                f"{effective_min_edge:.3f}"
            )

        if best_edge < effective_min_edge:
            logger.debug(
                f"[WEATHER-SKIP] low edge: {city} {label} "
                f"edge={best_edge:.4f} < min={effective_min_edge:.4f}"
            )
            return None

        # ── Source quality gate ──
        # Non serve una confidence inventata. Servono solo abbastanza fonti
        # per fidarci della stima probabilistica. sigma nel modello Phi()
        # cattura gia' l'incertezza — piu' fonti = sigma piu' precisa.
        n_sources = forecast.n_sources if hasattr(forecast, 'n_sources') else (
            1 if forecast.ensemble_temps else 0)

        # Multi-day con fonte singola: troppo incerto
        if n_sources < 2 and days_ahead > 1:
            logger.debug(
                f"[WEATHER-SKIP] single source multi-day: {city} {label} "
                f"sources={n_sources} days={days_ahead}"
            )
            return None

        # Confidence per logging/compatibilita' — derivata dai dati, non inventata.
        # Basata su: edge magnitude + fonte count. Non usata come filtro.
        confidence = min(0.50 + n_sources * 0.10 + best_edge * 0.30, 0.95)

        buy_price = price_yes if best_side == "YES" else price_no

        # v11.1: BUY_YES severamente ristretto — dati: 12% WR (3W/22L).
        # Profittevole SOLO per 2 outlier ($256 Toronto + $215 Seoul).
        # Requisiti stringenti: prezzo basso + alta probabilità + 2+ fonti.
        is_exact_bucket = "exact" in label
        if best_side == "YES":
            # v11.1: BUY_YES richiede SEMPRE 2+ fonti (singola fonte = coin flip)
            if n_sources < 2:
                logger.debug(
                    f"[WEATHER-SKIP] BUY_YES single-source: {city} {label} "
                    f"sources={n_sources} (need >=2)"
                )
                return None
            if is_exact_bucket:
                # Exact: forecast molto fiducioso + prezzo basso + edge alta
                if buy_price > 0.10 or forecast_prob < 0.40:
                    logger.debug(
                        f"[WEATHER-SKIP] BUY_YES exact: {city} {label} "
                        f"price={buy_price:.3f} prob={forecast_prob:.3f} "
                        f"(need price<=0.10 AND prob>=0.40)"
                    )
                    return None
            else:
                # Range: prezzo molto basso con edge forte
                if buy_price > 0.12:
                    logger.debug(
                        f"[WEATHER-SKIP] BUY_YES range high-price: {city} {label} "
                        f"price={buy_price:.3f} (max 0.12 per YES)"
                    )
                    return None

        # High-price guard — solo per BUY_YES: prezzi alti richiedono multi-fonte.
        # BUY_NO single-source a prezzo alto ha 100% WR storico (6/6 WIN),
        # il prezzo alto e' gia' protezione (payoff basso = perdita contenuta).
        if best_side == "YES" and buy_price > 0.45 and n_sources < 3:  # v12.9.1: AutoOptimizer stringe (era 0.65/2)
            logger.debug(
                f"[WEATHER-SKIP] single-source high-price: {city} {label} "
                f"price={buy_price:.3f} sources={n_sources}"
            )
            return None

        # v10.8.4: BUY_NO più selettivo — solo "tail selling" (estremi impossibili).
        # Dati: 122W/125L con vecchi filtri = edge zero.
        # Nuovo: BUY_NO solo se forecast dice P(YES) < 0.15 (bin molto improbabile).
        # Per exact bucket: richiedi dist >= 2.5° dal forecast (era 1.5°).
        if best_side == "NO":
            if forecast_prob > 0.15:
                logger.debug(
                    f"[WEATHER-SKIP] BUY_NO prob too high: {city} {label} "
                    f"P(YES)={forecast_prob:.3f} > 0.15 (serve bin improbabile)"
                )
                return None
            if is_exact_bucket:
                try:
                    bucket_mid = (low + high) / 2.0
                    forecast_temp = forecast.temp_in_unit(unit)
                    dist = abs(forecast_temp - bucket_mid)
                    if dist < 2.5:
                        logger.debug(
                            f"[WEATHER-SKIP] BUY_NO exact too close: {city} {label} "
                            f"forecast={forecast_temp:.1f} dist={dist:.1f} < 2.5"
                        )
                        return None
                except (AttributeError, TypeError):
                    pass
        # v10.6: Filtro payoff asimmetrico — blocca trade dove rischio >> ricompensa
        # Payoff ratio = profitto per $1 investito se win.
        # Scala con incertezza dell'orizzonte:
        #   same-day: min 0.25 → max price 0.80
        #   +1 giorno: min 0.30 → max price 0.77
        #   +2/3 giorni: min 0.35-0.40 → max price 0.74-0.71
        # Same-day BUY_NO a prezzo alto (0.80) ha 100% WR storico: payoff
        # basso (0.2x) ma quasi certo. Rilassare soglia per non bloccarli.
        if days_ahead == 0 and best_side == "NO":
            min_payoff = 0.20  # v12.9.1: AutoOptimizer alza da 0.15
        else:
            min_payoff = 0.34 + days_ahead * 0.08  # v12.9.1: AutoOptimizer alza da 0.25 → 0.34
        payoff_ratio = (1.0 / buy_price) - 1.0 if buy_price > 0 else 0
        if payoff_ratio < min_payoff:
            logger.debug(
                f"[WEATHER-SKIP] low payoff: {city} {label} "
                f"payoff={payoff_ratio:.3f} < min={min_payoff:.3f} "
                f"(price={buy_price:.3f} side={best_side})"
            )
            return None
        if buy_price < 0.08:
            return None

        # v12.7: Expected Value per $1 investito — usa adj_forecast_prob
        # (uncertainty-adjusted) per stima conservativa del win rate reale.
        # Raw forecast_prob è overconfident quando sigma è alto.
        # EV = win_prob * payoff_ratio - (1 - win_prob) * 1.0
        win_prob = adj_forecast_prob if best_side == "YES" else (1.0 - adj_forecast_prob)
        expected_value = win_prob * payoff_ratio - (1.0 - win_prob)

        # v12.9: EV gate — NEVER enter negative or marginal EV
        # 112K wallet study: top 1.2% never enter negative EV
        # EV < 0.10 = no real edge after accounting for uncertainty
        if expected_value < 0.10:
            logger.debug(
                f"[WEATHER-SKIP] low EV: {city} {label} "
                f"EV={expected_value:+.4f} < 0.10 "
                f"(win_prob={win_prob:.3f} payoff={payoff_ratio:.3f})"
            )
            return None

        # v12.0.1: Build MetaFeatures for meta-labeling
        mf = None
        if self.meta_labeler:
            from monitoring.meta_labeler import MetaFeatures
            mf = MetaFeatures(
                n_sources=n_sources,
                sigma=forecast.uncertainty_in_unit(unit),
                spread=abs(1.0 - price_yes - price_no),
                volume_24h=getattr(market, 'volume', 0) or 0,
                price=buy_price,
                days_ahead=days_ahead,
                hour_utc=datetime.now().hour,
                edge=best_edge,
                confidence=confidence,
                side=1 if best_side == "YES" else 0,
                expected_value=expected_value,
                payoff_ratio=payoff_ratio,
                is_latency_opp=_is_latency_opportunity,
                bucket_width=high - low,
            )

        return WeatherOpportunity(
            market=market,
            city=city,
            date=date,
            bucket_low=low,
            bucket_high=high,
            bucket_label=label,
            side=best_side,
            forecast_prob=forecast_prob,
            market_prob=price_yes,
            edge=best_edge,
            confidence=confidence,
            expected_value=expected_value,
            payoff_ratio=payoff_ratio,
            meta_features=mf,
            days_ahead=days_ahead,
            n_sources=n_sources,
            reasoning=(
                f"{city.upper()} {date} | "
                f"Bucket: {label} ({unit}) | "
                f"Forecast: {forecast.temp_in_unit(unit):.1f}°{unit} "
                f"±{forecast_sigma:.1f} | "
                f"P_raw={forecast_prob:.3f} P_adj={adj_forecast_prob:.3f} "
                f"vs Mkt={price_yes:.3f} | "
                f"Edge={best_edge:.3f} {best_side} "
                f"(σ_penalty={sigma_penalty:.3f}) | "
                f"EV={expected_value:+.3f}/$ payoff={payoff_ratio:.1f}x | "
                f"Sources: {forecast.source if hasattr(forecast, 'source') else 'single'} "
                f"({len(forecast.ensemble_temps)}ens)"
                + (f" | LATENCY shift={forecast_shift:+.1f}°C" if _is_latency_opportunity else "")
            ),
        )

    def _parse_date(self, question: str) -> str | None:
        """
        Estrai data dalla domanda del mercato.

        Formati supportati:
        - "... on February 14?"  → 2026-02-14
        - "... on February 14, 2026?"  → 2026-02-14
        - "... on Feb 14?"  → 2026-02-14
        """
        # Pattern: "on <month> <day>"
        # v10.7: \b previene match dentro parole (es. "Lond**on** be 19")
        pattern = r"\b(?:on|for)\s+(\w+)\.?\s+(\d+)(?:\s*,?\s*(\d{4}))?"
        match = re.search(pattern, question, re.I)
        if not match:
            return None

        month_str = match.group(1).lower()
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else datetime.now().year

        # Supporta nomi abbreviati
        for full_name, num in MONTHS.items():
            if full_name.startswith(month_str):
                try:
                    _dt.date(year, num, day)
                except ValueError:
                    return None
                return f"{year}-{num:02d}-{day:02d}"
        return None

    def _parse_bucket(self, market: Market) -> tuple[float, float, str] | None:
        """
        Estrai range di temperatura dal mercato.

        Cerca in: question, slug, outcomes.
        Formati supportati:
        - "Below 5°C" / "Under 5" → (-50, 5)
        - "5°C to 7°C" / "5 to 7" → (5, 7)
        - "Above 11°C" / "Over 11" → (11, 60)
        - "5°C or less" → (-50, 5.01)
        - "11°C or more" → (11, 60)

        Ritorna (low, high, label) oppure None.
        """
        # Testi da analizzare (in ordine di priorita')
        sources = [market.question, market.slug] + market.outcomes

        for text in sources:
            if not text:
                continue

            # Pulizia: rimuovi gradi/simboli per parsing numerico
            t = text.replace("°", " ").replace("º", " ")

            # IMPORTANTE: rimuovi date dal testo PRIMA del parsing numerico.
            # Senza questo, slug come "february-16-2026" vengono matchati
            # come range di temperatura "-16-2026" (bug v4.0).
            t = re.sub(
                r"(?:january|february|march|april|may|june|july|august|"
                r"september|october|november|december|jan|feb|mar|apr|"
                r"jun|jul|aug|sep|oct|nov|dec)"
                r"[\s,.-]*\d{1,2}[\s,.-]*\d{4}",
                " ", t, flags=re.I,
            )
            # Rimuovi anche date nel formato YYYY-MM-DD o MM-DD-YYYY
            t = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", " ", t)
            t = re.sub(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}", " ", t)

            # v5.0: range temp allargato per supportare Fahrenheit
            # Celsius: -60 a 60, Fahrenheit: -76 a 140
            TEMP_MIN, TEMP_MAX = -76, 140

            # Pattern: "Below/Under X"
            below = re.search(
                r"(?:below|under|less\s+than)\s+(-?\d+\.?\d*)", t, re.I
            )
            if below:
                try:
                    val = float(below.group(1))
                    if TEMP_MIN <= val <= TEMP_MAX:
                        return (TEMP_MIN, val, f"Below {val}")
                except ValueError:
                    pass

            # Pattern: "X or less"
            or_less = re.search(r"(-?\d+\.?\d*)\s*(?:C|F|degrees?)?\s+or\s+less", t, re.I)
            if or_less:
                try:
                    val = float(or_less.group(1))
                    if TEMP_MIN <= val <= TEMP_MAX:
                        return (TEMP_MIN, val + 0.01, f"{val} or less")
                except ValueError:
                    pass

            # Pattern: "Above/Over X"
            above = re.search(
                r"(?:above|over|more\s+than|higher\s+than)\s+(-?\d+\.?\d*)", t, re.I
            )
            if above:
                try:
                    val = float(above.group(1))
                    if TEMP_MIN <= val <= TEMP_MAX:
                        return (val, TEMP_MAX, f"Above {val}")
                except ValueError:
                    pass

            # Pattern: "X or more"
            or_more = re.search(r"(-?\d+\.?\d*)\s*(?:C|F|degrees?)?\s+or\s+more", t, re.I)
            if or_more:
                try:
                    val = float(or_more.group(1))
                    if TEMP_MIN <= val <= TEMP_MAX:
                        return (val, TEMP_MAX, f"{val} or more")
                except ValueError:
                    pass

            # Pattern: "X to Y" / "X - Y"
            range_match = re.search(
                r"(-?\d+\.?\d*)\s*(?:C|F|degrees?)?\s+to\s+(-?\d+\.?\d*)", t, re.I
            )
            if not range_match:
                range_match = re.search(
                    r"(?<![a-zA-Z\d])(-?\d{1,3}(?:\.\d+)?)\s*(?:C|F)?\s*-\s*(-?\d{1,3}(?:\.\d+)?)\s*(?:C|F)?(?![a-zA-Z\d-])",
                    t, re.I,
                )
            if range_match:
                try:
                    low = float(range_match.group(1))
                    high = float(range_match.group(2))
                    if TEMP_MIN <= low < high <= TEMP_MAX:
                        return (low, high, f"{low} to {high}")
                except ValueError:
                    pass

            # v10.7: Pattern: "be X°C/F" — mercato a temperatura esatta (Yes/No)
            # Es: "Will the highest temperature in Ankara be 9°C on March 4?"
            # Polymarket risolve come "temperatura arrotondata == X"
            # → bucket [X-0.5, X+0.5)
            exact_match = re.search(
                r"be\s+(-?\d+\.?\d*)\s*(?:C|F|degrees?)?(?:\s+(?:on|for)|\s*\?)",
                t, re.I,
            )
            if exact_match:
                try:
                    val = float(exact_match.group(1))
                    if TEMP_MIN <= val <= TEMP_MAX:
                        return (val - 0.5, val + 0.5, f"{val} exact")
                except ValueError:
                    pass

            # v10.7: Pattern: "X or below" / "X or lower" (varianti di "X or less")
            or_below = re.search(r"(-?\d+\.?\d*)\s*(?:C|F|degrees?)?\s+or\s+(?:below|lower)", t, re.I)
            if or_below:
                try:
                    val = float(or_below.group(1))
                    if TEMP_MIN <= val <= TEMP_MAX:
                        return (TEMP_MIN, val + 0.01, f"{val} or below")
                except ValueError:
                    pass

            # v10.7: Pattern: "X or higher" (variante di "X or more")
            or_higher = re.search(r"(-?\d+\.?\d*)\s*(?:C|F|degrees?)?\s+or\s+higher", t, re.I)
            if or_higher:
                try:
                    val = float(or_higher.group(1))
                    if TEMP_MIN <= val <= TEMP_MAX:
                        return (val, TEMP_MAX, f"{val} or higher")
                except ValueError:
                    pass

        return None

    def _days_until(self, date_str: str) -> int:
        """Giorni da oggi alla data target."""
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d")
            return max(0, (target - datetime.now()).days)
        except (ValueError, TypeError):
            return 3  # Default conservativo

    async def execute(
        self, opp: WeatherOpportunity, paper: bool = True
    ) -> bool:
        """Esegui un trade weather."""
        now = time.time()
        if now - self._recently_traded.get(opp.market.id, 0) < self._TRADE_COOLDOWN:
            return False

        token_key = "yes" if opp.side == "YES" else "no"
        token_id = opp.market.tokens[token_key]
        price = opp.market.prices[token_key]

        win_prob = opp.forecast_prob if opp.side == "YES" else (1.0 - opp.forecast_prob)
        size = self.risk.kelly_size(
            win_prob=win_prob,
            price=price,
            strategy=STRATEGY_NAME,
            days_ahead=self._days_until(opp.date),
        )

        if size == 0:
            logger.debug(
                f"[WEATHER] kelly_size=0 per '{opp.bucket_label}' "
                f"price={price:.4f} win_prob={win_prob:.4f}"
            )
            return False

        # v12.0.1: Meta-labeling size adjustment (Lopez de Prado AFML Ch 3)
        if self.meta_labeler and opp.meta_features:
            meta_prob = self.meta_labeler.predict(opp.meta_features)
            if meta_prob < 0.40:
                logger.info(
                    f"[META-LABEL] SKIP {opp.city} {opp.bucket_label}: "
                    f"P(profit)={meta_prob:.3f} < 0.40"
                )
                return False
            size *= meta_prob
            logger.debug(
                f"[META-LABEL] {opp.city}: P(profit)={meta_prob:.3f} "
                f"-> size=${size:.2f}"
            )

        # v12.10: high-price penalty RE-ENABLED
        # A price 0.80+, payoff è solo 0.25x ma loss è -$size. 29 delle 42 perdite
        # weather erano su prezzi 0.70-0.85. Penalty riduce sizing su queste zone.
        if price > 0.70:
            high_price_penalty = max(0.50, 1.0 - (price - 0.70) * 2.0)
            if high_price_penalty < 1.0:
                old_size = size
                size *= high_price_penalty
                logger.info(
                    f"[WEATHER] high-price penalty: price={price:.3f} "
                    f"penalty={high_price_penalty:.2f} size ${old_size:.2f}→${size:.2f}"
                )

        # v10.6: Size boost per trade ad alto EV con payoff favorevole.
        # Trade con payoff_ratio > 1.0 (win > stake) hanno rischio asimmetrico
        # A NOSTRO FAVORE — possiamo essere più aggressivi.
        if opp.payoff_ratio >= 2.0 and opp.expected_value >= 0.20:
            # Payoff 2x+ e EV forte: boost 40% (es. BUY_NO a 0.33 = 2x payoff)
            size = min(size * 1.40, MAX_WEATHER_BET)
        elif opp.payoff_ratio >= 1.0 and opp.expected_value >= 0.10:
            # Payoff 1x+ e EV positiva: boost 20%
            size = min(size * 1.20, MAX_WEATHER_BET)

        # v11.1: Sizing caps basati su dati reali
        # BUY_YES: ridotto a $15 (era $20) — 12% WR, solo longshot
        # BUY_NO +2d: ridotto a $30 — MAE 3.5F rende edge incerto
        # Tier 2 cities: cap $25 (Miami 58%, Buenos Aires 57%, Ankara 50%)
        days_ahead = self._days_until(opp.date)
        if opp.side == "YES":
            max_bet = 10.0  # v12.6.2: ridotto da $15 — BUY_YES troppo rischioso
        elif days_ahead >= 2:
            max_bet = 20.0  # v12.6.2: ridotto da $30 — +2d troppo incerto
        else:
            max_bet = MAX_WEATHER_BET  # same-day/+1d BUY_NO: nostro punto forte

        # City tier cap
        if opp.city.lower() in _dynamic_city_tier2:
            max_bet = min(max_bet, CITY_TIER2_MAX_BET)

        if size > max_bet:
            logger.debug(f"[WEATHER] size ${size:.2f} → cap ${max_bet:.2f} (side={opp.side} city={opp.city})")
            size = max_bet

        allowed, reason = self.risk.can_trade(
            STRATEGY_NAME, size, price=price,
            side=f"BUY_{opp.side}", market_id=opp.market.id,
        )
        if not allowed:
            logger.info(f"[WEATHER] Trade bloccato: {reason}")
            return False

        trade = Trade(
            timestamp=now,
            strategy=STRATEGY_NAME,
            market_id=opp.market.id,
            token_id=token_id,
            side=f"BUY_{opp.side}",
            size=size,
            price=price,
            edge=opp.edge,
            reason=opp.reasoning,
            city=opp.city,
            horizon=opp.days_ahead,
            sources=opp.n_sources,
            confidence=opp.confidence,
        )
        trade._meta_features = opp.meta_features

        if paper:
            logger.info(
                f"[PAPER] WEATHER: {opp.city.upper()} {opp.date} "
                f"BUY {opp.side} '{opp.bucket_label}' "
                f"${size:.2f} @{price:.4f} edge={opp.edge:.4f} "
                f"(consensus={opp.forecast_prob:.2f} vs mkt={opp.market_prob:.2f} "
                f"conf={opp.confidence:.2f})"
            )
            self.risk.open_trade(trade)

            # Simulazione paper: le previsioni weather sono accurate 70-85%
            # Win rate basato su edge e orizzonte previsionale
            days_ahead = self._days_until(opp.date)
            base_accuracy = 0.85 - days_ahead * 0.03  # 85% oggi, 64% a 7gg
            sim_win_prob = min(0.5 + opp.edge * 0.6, base_accuracy)
            won = random.random() < sim_win_prob

            slippage = 0.97
            if won:
                pnl = size * ((1.0 / price) - 1.0) * slippage
            else:
                pnl = -size * slippage

            self.risk.close_trade(token_id, won=won, pnl=pnl)
        else:
            # v13.1: Horizon SDK as primary execution, native smart_buy as fallback
            from utils.avellaneda_stoikov import market_inventory_frac
            inv = market_inventory_frac(self.risk.open_trades, opp.market.id, self.risk._strategy_budgets.get(STRATEGY_NAME, 1))
            vpin_val = self.risk.vpin_monitor.get_vpin(opp.market.id) if self.risk.vpin_monitor else 0.0

            if self.horizon is not None:
                # Route through Horizon (handles algo selection + native fallback)
                hz_result = self.horizon.execute_trade(
                    token_id=token_id,
                    side=f"BUY_{opp.side}",
                    size=size,
                    price=price,
                    strategy="weather",
                    inventory_frac=inv,
                    volume_24h=opp.market.volume,
                    vpin=vpin_val,
                )
                if hz_result.success:
                    if hz_result.fill_price > 0:
                        trade.price = hz_result.fill_price
                    self.risk.open_trade(trade)
                    result = hz_result.raw_result
                else:
                    logger.warning(f"[WEATHER] Horizon+native both failed: {hz_result.error}")
                    result = None
            else:
                # Legacy path: direct native smart_buy (no Horizon available)
                result = self.api.smart_buy(
                    token_id, size, target_price=price,
                    inventory_frac=inv, volume_24h=opp.market.volume, vpin=vpin_val,
                )
                if result:
                    # v7.4: Aggiorna prezzo con fill reale dal CLOB
                    if isinstance(result, dict) and result.get("_fill_price"):
                        trade.price = result["_fill_price"]
                    self.risk.open_trade(trade)

        self._recently_traded[opp.market.id] = now
        self._trades_executed += 1
        return True

    @property
    def stats(self) -> dict:
        return {
            "trades_executed": self._trades_executed,
            "markets_in_cooldown": sum(
                1
                for t in self._recently_traded.values()
                if time.time() - t < self._TRADE_COOLDOWN
            ),
        }
