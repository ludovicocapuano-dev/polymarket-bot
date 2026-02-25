"""
Strategia 5: Weather Prediction — v3.5 Multi-Source
=====================================================
Sfrutta le previsioni meteo MULTI-SORGENTE per tradare i mercati
weather su Polymarket: "Highest temperature in London on Feb 14?"

Provider (consensus pesato):
- Open-Meteo:  ensemble GFS 31 membri (peso 1.0)
- Wethr.net:   16+ modelli professionali (peso 1.5)
- NWS API:     previsioni ufficiali USA, fonte settlement (peso 0.8)

Approccio:
- Ogni provider fornisce una stima di probabilita' per bucket
- Il consensus fa media pesata delle probabilita' dei provider
- Se i provider divergono molto → incertezza aumentata → trade piu' cauti
- Confronta probabilita' consensus vs prezzo di mercato Polymarket
- Trada quando il consensus diverge significativamente dal mercato

Esempio (3 fonti):
  Mercato: "7°C to 9°C" in London Feb 14 — prezzo YES = $0.35
  Open-Meteo: P=0.68 (22/31 GFS membri)
  Wethr.net:  P=0.75 (12/16 modelli professionali)
  NWS:        N/A (non copre UK)
  Consensus:  0.72 → Edge = 0.72 - 0.35 = 0.37 → TRADE!
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

STRATEGY_NAME = "weather"
MAX_WEATHER_BET = 15.0  # Cap weather-specifico (loss da $25 troppo pesanti vs win medi)

# ── Pattern per riconoscere mercati weather ──────────────────
WEATHER_PATTERNS = [
    re.compile(r"(?:highest|high|max)\s+temp", re.I),
    re.compile(r"temperature\s+in\s+\w+", re.I),
    re.compile(r"(?:lowest|low|min)\s+temp", re.I),
    # v5.0: pattern extra per catturare piu' mercati weather
    re.compile(r"(?:rain|snow|precipitation)\s+in\s+\w+", re.I),
    re.compile(r"(?:london|nyc|chicago|seoul|miami|ankara|seattle|atlanta|dallas).*(?:temp|degree|°)", re.I),
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
        min_confidence: float = 0.45,
    ):
        self.api = api
        self.risk = risk
        self.weather = weather
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self._trades_executed = 0
        self._recently_traded: dict[str, float] = {}
        self._TRADE_COOLDOWN = 600  # v5.0: 10 min cooldown (niche dominance)
        self._prefetch_done = False

    async def scan(
        self, shared_markets: list[Market] | None = None
    ) -> list[WeatherOpportunity]:
        """Scansiona mercati weather per opportunita'."""
        markets = shared_markets or self.api.fetch_markets(limit=200)
        if not markets:
            return []

        # Filtra mercati weather
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

        opportunities.sort(key=lambda o: o.edge * o.confidence, reverse=True)

        if opportunities:
            logger.info(
                f"[WEATHER] Scan {len(markets)} mercati "
                f"({len(weather_markets)} weather, {skipped_cooldown} cooldown, "
                f"{skipped_parse} parse-fail) → "
                f"{len(opportunities)} opportunita' "
                f"(migliore: {opportunities[0].city.upper()} "
                f"'{opportunities[0].bucket_label}' "
                f"edge={opportunities[0].edge:.4f})"
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
            return None

        # 2. Rileva data
        date = self._parse_date(q)
        if not date:
            return None

        # 3. Ottieni previsione
        forecast = self.weather.get_forecast_for_date(city, date)
        if not forecast:
            return None

        # 4. Rileva bucket di temperatura
        bucket = self._parse_bucket(market)
        if not bucket:
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

        # 6. Confronta con prezzo di mercato
        price_yes = market.prices.get("yes", 0.5)
        price_no = market.prices.get("no", 0.5)

        # Fee deduction: non-crypto markets use ~0.005 fee rate
        fee = 0.0  # weather markets sono fee-free su Polymarket
        edge_yes = forecast_prob - price_yes - fee
        edge_no = (1.0 - forecast_prob) - price_no - fee

        best_side = "YES" if edge_yes > edge_no else "NO"
        best_edge = max(edge_yes, edge_no)

        # v8.0: MIN_EDGE ridotto per same-day weather (previsioni molto piu' accurate)
        days_ahead_edge = self._days_until(date)
        effective_min_edge = 0.02 if days_ahead_edge == 0 else self.min_edge
        if best_edge < effective_min_edge:
            return None

        # v7.0: Filtro rischio asimmetrico su prezzi estremi.
        # BUY_NO a 0.93 = rischia $23.25 per guadagnare $1.75 (risk/reward 13:1)
        # Anche con 90% accuratezza: EV = 0.9*1.75 - 0.1*23.25 = -$0.75 NEGATIVO
        # Blocca trade dove il risk/reward supera 5:1
        # 7. Confidence basata sulla qualita' della previsione
        # - Orizzonte vicino = piu' affidabile
        # - Piu' fonti = piu' affidabile (consensus multi-provider)
        # - Ensemble presente = piu' affidabile
        days_ahead = self._days_until(date)
        horizon_conf = max(0.3, 1.0 - days_ahead * 0.1)  # 1.0 per oggi, 0.3 per 7gg

        # Bonus per numero di fonti (1 fonte=0.6, 2=0.8, 3+=0.9)
        n_sources = forecast.n_sources if hasattr(forecast, 'n_sources') else (1 if forecast.ensemble_temps else 0)
        source_conf = min(0.5 + n_sources * 0.15, 0.9)

        # Ensemble boost (se abbiamo dati probabilistici, non solo punto)
        has_ensemble = bool(forecast.ensemble_temps)
        ensemble_boost = 1.0 if has_ensemble else 0.8

        confidence = min(horizon_conf * source_conf * ensemble_boost, 0.90)

        if confidence < self.min_confidence:
            return None

        buy_price = price_yes if best_side == "YES" else price_no
        if buy_price > 0.85:
            # v8.0: Rilassato — Becker: Weather ha bias positivo (underpriced) a 0.60-0.90
            # Permettere se edge forte E confidence alta
            if best_edge < 0.05 or confidence < 0.75:
                # Blocca ancora se edge/confidence deboli
                return None
            # Altrimenti procedi — Becker conferma underpricing a 0.85-0.90
        if buy_price < 0.05:
            # Longshot: paghi poco ma probabilita' troppo bassa
            return None

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
            reasoning=(
                f"{city.upper()} {date} | "
                f"Bucket: {label} ({unit}) | "
                f"Forecast: {forecast.temp_in_unit(unit):.1f}°{unit} "
                f"±{forecast.uncertainty_in_unit(unit):.1f} | "
                f"P_consensus={forecast_prob:.3f} vs Mkt={price_yes:.3f} | "
                f"Edge={best_edge:.3f} {best_side} | "
                f"Sources: {forecast.source if hasattr(forecast, 'source') else 'single'} "
                f"({len(forecast.ensemble_temps)}ens)"
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
        pattern = r"(?:on|for)\s+(\w+)\.?\s+(\d+)(?:\s*,?\s*(\d{4}))?"
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
        )

        if size == 0:
            logger.debug(
                f"[WEATHER] kelly_size=0 per '{opp.bucket_label}' "
                f"price={price:.4f} win_prob={win_prob:.4f}"
            )
            return False

        if size > MAX_WEATHER_BET:
            logger.debug(f"[WEATHER] size ${size:.2f} → cap ${MAX_WEATHER_BET:.2f}")
            size = MAX_WEATHER_BET

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
        )

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
            result = self.api.buy_market(token_id, size)
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
