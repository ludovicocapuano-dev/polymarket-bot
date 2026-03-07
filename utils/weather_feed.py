"""
Feed previsioni meteo multi-sorgente per weather trading — v3.6

Architettura a 4 provider con consensus pesato:
 1. Open-Meteo          — ensemble GFS 31 membri (gratuito, no API key)
 2. Wethr.net           — 16+ modelli professionali (API key, $)
 3. NWS API             — previsioni ufficiali USA (gratuito, solo NYC)
 4. Weather Underground — fonte di settlement Polymarket (API key, free con PWS)

Le probabilita' per bucket vengono calcolate come media pesata
dei singoli provider, ognuno con peso proporzionale alla qualita':
  WeatherUnderground x2.0 (fonte di settlement Polymarket — peso massimo)
  Wethr.net  x1.5 (multi-model, resolution-specific)
  Open-Meteo x1.0 (ensemble GFS, buona copertura)
  NWS        x0.8 (singolo modello, fonte ufficiale USA)

API:
  Ensemble:  https://ensemble-api.open-meteo.com/v1/ensemble
  Standard:  https://api.open-meteo.com/v1/forecast
  Wethr:     https://wethr.net/api/v2/
  NWS:       https://api.weather.gov/
  WU:        https://api.weather.com/v3/wx/
"""

import logging
import math
import os
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

# ── Citta' supportate ─────────────────────────────────────────
# Coordinate + station codes per ogni provider
WEATHER_CITIES: dict[str, dict] = {
    "london": {
        "lat": 51.505,
        "lon": -0.055,
        "keywords": ["london"],
        "station": "London City Airport",
        "wethr_code": "EGLC",       # London City Airport
        "nws_grid": None,
        "unit": "C",                 # Risolve in Celsius
    },
    "nyc": {
        "lat": 40.782,
        "lon": -73.967,
        "keywords": ["new york", "nyc", "manhattan", "laguardia"],
        "station": "LaGuardia Airport",
        "wethr_code": "KLGA",        # LaGuardia — fonte settlement Polymarket
        "nws_grid": ("OKX", 33, 37),
        "unit": "F",                 # Polymarket NYC risolve in Fahrenheit!
    },
    "chicago": {
        "lat": 41.974,
        "lon": -87.907,
        "keywords": ["chicago", "o'hare"],
        "station": "Chicago O'Hare Intl",
        "wethr_code": "KORD",
        "nws_grid": ("LOT", 65, 76),
        "unit": "F",
    },
    "seoul": {
        "lat": 37.449,
        "lon": 126.451,
        "keywords": ["seoul", "incheon"],
        "station": "Incheon Intl Airport",
        "wethr_code": "RKSI",
        "nws_grid": None,
        "unit": "C",
    },
    "miami": {
        "lat": 25.795,
        "lon": -80.290,
        "keywords": ["miami"],
        "station": "Miami Intl Airport",
        "wethr_code": "KMIA",
        "nws_grid": ("MFL", 75, 54),
        "unit": "F",
    },
    "ankara": {
        "lat": 40.128,
        "lon": 32.995,
        "keywords": ["ankara"],
        "station": "Esenboga Airport",
        "wethr_code": "LTAC",
        "nws_grid": None,
        "unit": "C",
    },
    "seattle": {
        "lat": 47.449,
        "lon": -122.309,
        "keywords": ["seattle", "tacoma"],
        "station": "Seattle-Tacoma Intl",
        "wethr_code": "KSEA",
        "nws_grid": ("SEW", 124, 67),
        "unit": "F",
    },
    "atlanta": {
        "lat": 33.640,
        "lon": -84.427,
        "keywords": ["atlanta"],
        "station": "Hartsfield-Jackson Intl",
        "wethr_code": "KATL",
        "nws_grid": ("FFC", 52, 88),
        "unit": "F",
    },
    "dallas": {
        "lat": 32.847,
        "lon": -96.851,
        "keywords": ["dallas", "fort worth"],
        "station": "Dallas/Fort Worth Intl",
        "wethr_code": "KDFW",
        "nws_grid": ("FWD", 80, 108),
        "unit": "F",
    },
    # v5.3: nuove citta' da mercati Polymarket attivi
    "sao paulo": {
        "lat": -23.626,
        "lon": -46.655,
        "keywords": ["sao paulo", "são paulo", "saopaulo"],
        "station": "Congonhas Airport",
        "wethr_code": "SBSP",
        "nws_grid": None,
        "unit": "C",
    },
    "toronto": {
        "lat": 43.677,
        "lon": -79.631,
        "keywords": ["toronto"],
        "station": "Toronto Pearson Intl",
        "wethr_code": "CYYZ",
        "nws_grid": None,
        "unit": "C",
    },
    "paris": {
        "lat": 49.010,
        "lon": 2.548,
        "keywords": ["paris"],
        "station": "Charles de Gaulle Airport",
        "wethr_code": "LFPG",
        "nws_grid": None,
        "unit": "C",
    },
    "buenos aires": {
        "lat": -34.560,
        "lon": -58.416,
        "keywords": ["buenos aires"],
        "station": "Aeroparque Jorge Newbery",
        "wethr_code": "SABE",
        "nws_grid": None,
        "unit": "C",
    },
    # v10.7: città aggiunte per coprire mercati Polymarket attivi
    "tokyo": {
        "lat": 35.765,
        "lon": 140.386,
        "keywords": ["tokyo", "narita"],
        "station": "Narita Intl",
        "wethr_code": "RJAA",
        "nws_grid": None,
        "unit": "C",
    },
    "sydney": {
        "lat": -33.946,
        "lon": 151.177,
        "keywords": ["sydney"],
        "station": "Sydney Kingsford Smith",
        "wethr_code": "YSSY",
        "nws_grid": None,
        "unit": "C",
    },
    "denver": {
        "lat": 39.856,
        "lon": -104.674,
        "keywords": ["denver"],
        "station": "Denver Intl",
        "wethr_code": "KDEN",
        "nws_grid": ("BOU", 62, 60),
        "unit": "F",
    },
    "los angeles": {
        "lat": 33.943,
        "lon": -118.408,
        "keywords": ["los angeles", "la ", "lax"],
        "station": "Los Angeles Intl",
        "wethr_code": "KLAX",
        "nws_grid": ("LOX", 149, 48),
        "unit": "F",
    },
    "phoenix": {
        "lat": 33.437,
        "lon": -112.008,
        "keywords": ["phoenix"],
        "station": "Phoenix Sky Harbor",
        "wethr_code": "KPHX",
        "nws_grid": ("PSR", 160, 57),
        "unit": "F",
    },
    "houston": {
        "lat": 29.990,
        "lon": -95.336,
        "keywords": ["houston"],
        "station": "George Bush Intl",
        "wethr_code": "KIAH",
        "nws_grid": ("HGX", 65, 97),
        "unit": "F",
    },
    "wellington": {
        "lat": -41.327,
        "lon": 174.805,
        "keywords": ["wellington"],
        "station": "Wellington Airport",
        "wethr_code": "NZWN",
        "nws_grid": None,
        "unit": "C",
    },
    "lucknow": {
        "lat": 26.846,
        "lon": 80.946,
        "keywords": ["lucknow"],
        "station": "Amausi Airport",
        "wethr_code": "VILK",
        "nws_grid": None,
        "unit": "C",
    },
    "munich": {
        "lat": 48.354,
        "lon": 11.786,
        "keywords": ["munich", "münchen"],
        "station": "Munich Airport",
        "wethr_code": "EDDM",
        "nws_grid": None,
        "unit": "C",
    },
}

# ── URL API ───────────────────────────────────────────────────
OPENMETEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPENMETEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WETHR_BASE_URL = "https://wethr.net/api/v2"
NWS_BASE_URL = "https://api.weather.gov"
WU_BASE_URL = "https://api.weather.com"

CACHE_DURATION = 1200  # v5.0: 20 min (piu' aggressivo per niche dominance)


# ── Math helpers ──────────────────────────────────────────────

def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """CDF della distribuzione normale (senza scipy)."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def c_to_f(c: float) -> float:
    """Celsius -> Fahrenheit."""
    return c * 9.0 / 5.0 + 32.0


def f_to_c(f: float) -> float:
    """Fahrenheit -> Celsius."""
    return (f - 32.0) * 5.0 / 9.0


def get_city_unit(city: str) -> str:
    """Unita di misura per la citta (come usata da Polymarket settlement)."""
    info = WEATHER_CITIES.get(city, {})
    return info.get("unit", "C")


# ── Dataclass ─────────────────────────────────────────────────

@dataclass
class SourceForecast:
    """Previsione da un singolo provider."""
    provider: str           # "open_meteo", "wethr", "nws"
    temp: float             # temperatura media prevista
    uncertainty: float      # deviazione standard stimata
    ensemble_temps: list[float]  # temperature individuali (modelli/membri)
    weight: float           # peso per consensus (qualita' del provider)

    def bucket_probability(self, low: float, high: float) -> float:
        """Probabilita' che la temperatura cada in [low, high) per questo provider."""
        if self.ensemble_temps and len(self.ensemble_temps) >= 3:
            count = sum(1 for t in self.ensemble_temps if low <= t < high)
            return (count + 0.5) / (len(self.ensemble_temps) + 1)
        else:
            if self.uncertainty <= 0:
                return 1.0 if low <= self.temp < high else 0.0
            p_high = _normal_cdf(high, self.temp, self.uncertainty)
            p_low = _normal_cdf(low, self.temp, self.uncertainty)
            return max(0.0, p_high - p_low)


@dataclass
class CityForecast:
    """
    Previsione consolidata per una citta' in un giorno specifico.

    Combina dati da piu' provider in una probabilita' consensus.
    Mantiene interfaccia backward-compatible con v3.4.
    """
    city: str
    date: str                      # "2026-02-14"
    forecast_temp: float           # media pesata da tutti i provider
    ensemble_temps: list[float]    # ensemble merged (per backward compat)
    uncertainty: float             # incertezza media pesata
    sources: list[SourceForecast] = field(default_factory=list)
    updated_at: float = 0.0

    @property
    def source(self) -> str:
        """Nomi provider (retrocompatibile con v3.4)."""
        if self.sources:
            return "+".join(s.provider for s in self.sources)
        return "unknown"

    def temp_in_unit(self, unit: str) -> float:
        """Temperatura nella unit richiesta (C o F). Internamente tutto e' in C."""
        if unit == "F":
            return c_to_f(self.forecast_temp)
        return self.forecast_temp

    def uncertainty_in_unit(self, unit: str) -> float:
        """Incertezza nella unit richiesta. Scala per F e' 1.8x."""
        if unit == "F":
            return self.uncertainty * 1.8
        return self.uncertainty

    def bucket_probability_in_unit(self, low: float, high: float, unit: str) -> float:
        """Probabilita per bucket espresso nell'unita del mercato."""
        if unit == "F":
            low_c = f_to_c(low)
            high_c = f_to_c(high)
            return self.bucket_probability(low_c, high_c)
        return self.bucket_probability(low, high)

    @property
    def n_sources(self) -> int:
        return len(self.sources)

    def bucket_probability(self, low: float, high: float) -> float:
        """
        Probabilita' consensus che la temperatura cada nel range [low, high).

        Se abbiamo piu' provider, fa media pesata delle loro probabilita'.
        Altrimenti fallback al metodo v3.4 (ensemble diretto o CDF).
        """
        if self.sources:
            total_weight = 0.0
            weighted_prob = 0.0
            for src in self.sources:
                p = src.bucket_probability(low, high)
                weighted_prob += p * src.weight
                total_weight += src.weight
            if total_weight > 0:
                return weighted_prob / total_weight

        # Fallback v3.4: usa ensemble merged o CDF
        if self.ensemble_temps and len(self.ensemble_temps) >= 5:
            count = sum(1 for t in self.ensemble_temps if low <= t < high)
            return (count + 0.5) / (len(self.ensemble_temps) + 1)
        else:
            if self.uncertainty <= 0:
                return 1.0 if low <= self.forecast_temp < high else 0.0
            p_high = _normal_cdf(high, self.forecast_temp, self.uncertainty)
            p_low = _normal_cdf(low, self.forecast_temp, self.uncertainty)
            return max(0.0, p_high - p_low)


# ══════════════════════════════════════════════════════════════
#  Provider: Open-Meteo (gratuito, ensemble GFS 31 membri)
# ══════════════════════════════════════════════════════════════

class OpenMeteoProvider:
    """Fetch previsioni ensemble GFS da Open-Meteo (gratuito)."""

    WEIGHT = 1.0  # peso base

    def __init__(self, session: requests.Session):
        self._session = session

    def fetch(self, city: str) -> dict[str, SourceForecast]:
        """Ritorna dict date → SourceForecast per una citta'."""
        info = WEATHER_CITIES.get(city)
        if not info:
            return {}

        # Prova ensemble, poi fallback deterministico
        result = self._fetch_ensemble(city, info)
        if not result:
            result = self._fetch_deterministic(city, info)
        return result

    def _fetch_ensemble(self, city: str, info: dict) -> dict[str, SourceForecast]:
        try:
            resp = self._session.get(
                OPENMETEO_ENSEMBLE_URL,
                params={
                    "latitude": info["lat"],
                    "longitude": info["lon"],
                    "daily": "temperature_2m_max",
                    "models": "gfs_seamless",
                    "forecast_days": 7,
                    "past_days": 1,
                    "timezone": "UTC",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.debug(f"[OPENMETEO] Ensemble {resp.status_code} per {city}")
                return {}

            data = resp.json()
            daily = data.get("daily", {})
            dates = daily.get("time", [])
            if not dates:
                return {}

            results: dict[str, SourceForecast] = {}
            for i, date in enumerate(dates):
                members: list[float] = []
                for key, values in daily.items():
                    if key.startswith("temperature_2m_max") and key != "temperature_2m_max":
                        if i < len(values) and values[i] is not None:
                            members.append(float(values[i]))

                if not members:
                    continue

                mean_temp = sum(members) / len(members)
                variance = sum((t - mean_temp) ** 2 for t in members) / len(members)
                std = max(variance ** 0.5, 0.3)

                results[date] = SourceForecast(
                    provider="open_meteo",
                    temp=round(mean_temp, 1),
                    uncertainty=round(std, 2),
                    ensemble_temps=members,
                    weight=self.WEIGHT,
                )

            if results:
                first = next(iter(results.values()))
                logger.info(
                    f"[OPENMETEO] {city}: {len(results)} giorni, "
                    f"{len(first.ensemble_temps)} membri, "
                    f"oggi={first.temp:.1f}°C ±{first.uncertainty:.1f}"
                )
            return results

        except Exception as e:
            logger.debug(f"[OPENMETEO] Errore ensemble {city}: {e}")
            return {}

    def _fetch_deterministic(self, city: str, info: dict) -> dict[str, SourceForecast]:
        try:
            resp = self._session.get(
                OPENMETEO_FORECAST_URL,
                params={
                    "latitude": info["lat"],
                    "longitude": info["lon"],
                    "daily": "temperature_2m_max",
                    "forecast_days": 7,
                    "past_days": 1,
                    "timezone": "UTC",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return {}

            data = resp.json()
            dates = data.get("daily", {}).get("time", [])
            temps = data.get("daily", {}).get("temperature_2m_max", [])

            results: dict[str, SourceForecast] = {}
            for i, (date, temp) in enumerate(zip(dates, temps)):
                if temp is None:
                    continue
                sigma = 1.0 + i * 0.3

                results[date] = SourceForecast(
                    provider="open_meteo",
                    temp=round(float(temp), 1),
                    uncertainty=round(sigma, 2),
                    ensemble_temps=[],
                    weight=self.WEIGHT * 0.7,  # peso ridotto senza ensemble
                )

            if results:
                first = next(iter(results.values()))
                logger.info(
                    f"[OPENMETEO] Deterministico {city}: {len(results)} giorni, "
                    f"oggi={first.temp:.1f}°C"
                )
            return results

        except Exception as e:
            logger.warning(f"[OPENMETEO] Errore forecast {city}: {e}")
            return {}


# ══════════════════════════════════════════════════════════════
#  Provider: Wethr.net (multi-model professionale, API key)
# ══════════════════════════════════════════════════════════════

class WethrProvider:
    """
    Fetch previsioni multi-modello da Wethr.net API v2.

    Endpoint usati:
    - /api/v2/nws_forecasts.php  — previsioni NWS per stazione
    - /api/v2/model_forecasts.php — temperature da 16+ modelli
    - /api/v2/observations.php   — osservazioni reali in tempo reale

    Autenticazione: X-API-Key header
    """

    WEIGHT = 1.5  # peso alto: multi-model professionale

    def __init__(self, session: requests.Session, api_key: str):
        self._session = session
        self._api_key = api_key
        self._headers = {"X-API-Key": api_key}
        self._available = bool(api_key)

    @property
    def available(self) -> bool:
        return self._available

    def fetch(self, city: str) -> dict[str, SourceForecast]:
        """Ritorna dict date → SourceForecast per una citta'."""
        if not self._available:
            return {}

        info = WEATHER_CITIES.get(city)
        if not info:
            return {}

        station = info.get("wethr_code")
        if not station:
            return {}

        # Prova model_forecasts (multi-modello), poi fallback a nws_forecasts
        result = self._fetch_model_forecasts(city, station)
        if not result:
            result = self._fetch_nws_forecasts(city, station)
        return result

    def _fetch_model_forecasts(self, city: str, station: str) -> dict[str, SourceForecast]:
        """Fetch temperature da 16+ modelli meteorologici."""
        try:
            resp = self._session.get(
                f"{WETHR_BASE_URL}/model_forecasts.php",
                params={"station_code": station},
                headers=self._headers,
                timeout=15,
            )

            if resp.status_code == 401:
                logger.warning("[WETHR] API key non valida o scaduta")
                self._available = False
                return {}

            if resp.status_code == 403:
                logger.warning(
                    f"[WETHR] Accesso negato per {station} "
                    f"(potrebbe richiedere piano superiore)"
                )
                return {}

            if resp.status_code != 200:
                logger.debug(f"[WETHR] model_forecasts {resp.status_code} per {station}")
                return {}

            data = resp.json()

            # Wethr.net model_forecasts: aspettiamo un array di modelli con temperature
            # Formato stimato basato su documentazione:
            # { "station": "KJFK",
            #   "forecasts": [
            #     { "date": "2026-02-14", "models": {
            #         "gfs": 8.5, "ecmwf": 9.0, "hrrr": 8.8, "nam": 8.7, ...
            #     }}
            #   ]
            # }
            # Se il formato e' diverso, il try/except gestisce il fallback.

            results: dict[str, SourceForecast] = {}

            # Prova formato con "forecasts" array
            forecasts_data = data.get("forecasts", data.get("data", []))
            if isinstance(forecasts_data, list):
                for entry in forecasts_data:
                    date = entry.get("date", entry.get("valid_date", ""))
                    if not date:
                        continue

                    # Raccoglie temperature da tutti i modelli disponibili
                    models = entry.get("models", entry.get("model_data", {}))
                    if isinstance(models, dict):
                        model_temps = [
                            float(v) for v in models.values()
                            if v is not None and self._is_temp(v)
                        ]
                    elif isinstance(models, list):
                        model_temps = [
                            float(m.get("high", m.get("temperature", m.get("temp", 0))))
                            for m in models
                            if isinstance(m, dict) and m.get("high", m.get("temperature", m.get("temp"))) is not None
                        ]
                    else:
                        continue

                    if len(model_temps) < 2:
                        continue

                    mean_t = sum(model_temps) / len(model_temps)
                    var = sum((t - mean_t) ** 2 for t in model_temps) / len(model_temps)
                    std = max(var ** 0.5, 0.3)

                    results[date] = SourceForecast(
                        provider="wethr",
                        temp=round(mean_t, 1),
                        uncertainty=round(std, 2),
                        ensemble_temps=model_temps,
                        weight=self.WEIGHT,
                    )

            if results:
                first = next(iter(results.values()))
                logger.info(
                    f"[WETHR] Models {city}/{station}: {len(results)} giorni, "
                    f"{len(first.ensemble_temps)} modelli, "
                    f"oggi={first.temp:.1f}°C ±{first.uncertainty:.1f}"
                )
            return results

        except Exception as e:
            logger.debug(f"[WETHR] Errore model_forecasts {station}: {e}")
            return {}

    def _fetch_nws_forecasts(self, city: str, station: str) -> dict[str, SourceForecast]:
        """Fallback: previsioni NWS via Wethr.net."""
        try:
            resp = self._session.get(
                f"{WETHR_BASE_URL}/nws_forecasts.php",
                params={"station_code": station},
                headers=self._headers,
                timeout=15,
            )

            if resp.status_code != 200:
                logger.debug(f"[WETHR] nws_forecasts {resp.status_code} per {station}")
                return {}

            data = resp.json()
            results: dict[str, SourceForecast] = {}

            # Formato atteso: array di previsioni orarie/giornaliere
            forecasts = data.get("forecasts", data.get("data", []))
            if isinstance(forecasts, list):
                # Raggruppa per data e prendi la max temperature
                daily_temps: dict[str, list[float]] = {}
                for entry in forecasts:
                    date = str(entry.get("date", entry.get("valid_date", "")))[:10]
                    temp = entry.get("temperature", entry.get("high", entry.get("temp")))
                    if date and temp is not None:
                        daily_temps.setdefault(date, []).append(float(temp))

                for date, temps in daily_temps.items():
                    max_t = max(temps)
                    # Con singolo modello, peso ridotto
                    results[date] = SourceForecast(
                        provider="wethr",
                        temp=round(max_t, 1),
                        uncertainty=1.5,  # stima conservativa
                        ensemble_temps=[],
                        weight=self.WEIGHT * 0.6,
                    )

            if results:
                first = next(iter(results.values()))
                logger.info(
                    f"[WETHR] NWS fallback {city}/{station}: {len(results)} giorni, "
                    f"oggi={first.temp:.1f}°C"
                )
            return results

        except Exception as e:
            logger.debug(f"[WETHR] Errore nws_forecasts {station}: {e}")
            return {}

    def fetch_observations(self, city: str) -> dict | None:
        """
        Fetch osservazioni in tempo reale (utile per mercati same-day).

        Ritorna l'ultima osservazione: temperatura corrente, max/min oggi, ecc.
        """
        if not self._available:
            return None

        info = WEATHER_CITIES.get(city)
        station = info.get("wethr_code") if info else None
        if not station:
            return None

        try:
            resp = self._session.get(
                f"{WETHR_BASE_URL}/observations.php",
                params={"station_code": station},
                headers=self._headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.debug(f"[WETHR] Observations {station}: {data}")
                return data
            return None
        except Exception as e:
            logger.debug(f"[WETHR] Errore observations {station}: {e}")
            return None

    @staticmethod
    def _is_temp(val) -> bool:
        """Verifica se un valore sembra una temperatura plausibile."""
        try:
            t = float(val)
            return -60 < t < 65  # range temperature terrestri
        except (ValueError, TypeError):
            return False


# ══════════════════════════════════════════════════════════════
#  Provider: NWS API (gratuito, solo USA)
# ══════════════════════════════════════════════════════════════

class NWSProvider:
    """
    Fetch previsioni da National Weather Service (api.weather.gov).

    Gratuito, senza API key. Solo stazioni USA (NYC nel nostro caso).
    IMPORTANTE: NWS e' la fonte di settlement per i mercati USA su Polymarket.
    """

    WEIGHT = 0.8  # peso: singolo modello ma fonte ufficiale di settlement

    def __init__(self, session: requests.Session):
        self._session = session
        self._headers = {
            "User-Agent": "(polymarket-weather-bot, contact@example.com)",
            "Accept": "application/geo+json",
        }

    def fetch(self, city: str) -> dict[str, SourceForecast]:
        """Ritorna dict date → SourceForecast (solo citta' USA)."""
        info = WEATHER_CITIES.get(city)
        if not info:
            return {}

        grid = info.get("nws_grid")
        if not grid:
            return {}  # NWS solo USA

        office, grid_x, grid_y = grid
        return self._fetch_gridpoint_forecast(city, office, grid_x, grid_y)

    def _fetch_gridpoint_forecast(
        self, city: str, office: str, grid_x: int, grid_y: int
    ) -> dict[str, SourceForecast]:
        """Fetch forecast dal gridpoint NWS."""
        try:
            url = f"{NWS_BASE_URL}/gridpoints/{office}/{grid_x},{grid_y}/forecast"
            resp = self._session.get(url, headers=self._headers, timeout=15)

            if resp.status_code != 200:
                logger.debug(f"[NWS] Forecast {resp.status_code} per {city}")
                return {}

            data = resp.json()
            periods = data.get("properties", {}).get("periods", [])

            # NWS restituisce periodi "Daytime" e "Nighttime" in Fahrenheit
            results: dict[str, SourceForecast] = {}
            for period in periods:
                if not period.get("isDaytime", False):
                    continue  # Solo periodi diurni (high temp)

                # Estrai data dal startTime "2026-02-14T06:00:00-05:00"
                start = period.get("startTime", "")
                date = start[:10] if len(start) >= 10 else ""
                if not date:
                    continue

                temp_f = period.get("temperature")
                if temp_f is None:
                    continue

                # Converti F → C
                temp_c = (float(temp_f) - 32) * 5 / 9

                # NWS non da' ensemble, generiamo incertezza basata su orizzonte
                # Primo giorno: σ ≈ 1.0°C, crescente
                from datetime import datetime
                try:
                    target = datetime.strptime(date, "%Y-%m-%d")
                    days_ahead = max(0, (target - datetime.now()).days)
                except (ValueError, TypeError):
                    days_ahead = 3
                sigma = 1.0 + days_ahead * 0.25

                results[date] = SourceForecast(
                    provider="nws",
                    temp=round(temp_c, 1),
                    uncertainty=round(sigma, 2),
                    ensemble_temps=[],  # NWS non ha ensemble
                    weight=self.WEIGHT,
                )

            if results:
                first = next(iter(results.values()))
                logger.info(
                    f"[NWS] Forecast {city}: {len(results)} giorni, "
                    f"oggi={first.temp:.1f}°C (fonte settlement)"
                )
            return results

        except Exception as e:
            logger.debug(f"[NWS] Errore forecast {city}: {e}")
            return {}


# ══════════════════════════════════════════════════════════════
#  Provider: Weather Underground (fonte settlement Polymarket)
# ══════════════════════════════════════════════════════════════

class WeatherUndergroundProvider:
    """
    Fetch previsioni da Weather Underground / IBM Weather Company.

    CRITICO: Weather Underground e' la fonte che Polymarket usa per il
    settlement dei weather markets. Allineare le nostre previsioni con WU
    riduce il rischio di discrepanza tra forecast e settlement.

    API: api.weather.com (v3 e v1 con fallback).
    Auth: apiKey parameter (free con PWS, $$ senza).
    Env var: WUNDERGROUND_API_KEY
    """

    WEIGHT = 2.0  # Peso massimo: fonte di settlement Polymarket

    def __init__(self, session: requests.Session, api_key: str):
        self._session = session
        self._api_key = api_key
        self._available = bool(api_key)
        self._consecutive_errors = 0

    @property
    def available(self) -> bool:
        return self._available

    def fetch(self, city: str) -> dict[str, SourceForecast]:
        """Ritorna dict date → SourceForecast per una citta'."""
        if not self._available:
            return {}

        info = WEATHER_CITIES.get(city)
        if not info:
            return {}

        # v3 prima, v1 come fallback
        result = self._fetch_v3(city, info)
        if not result:
            result = self._fetch_v1(city, info)
        return result

    def fetch_observations(self, city: str) -> dict | None:
        """
        Fetch osservazioni correnti da WU (per same-day markets).

        Ritorna dict con temperature corrente e max/min odierne.
        Utile per confrontare con il settlement.
        """
        if not self._available:
            return None

        info = WEATHER_CITIES.get(city)
        if not info:
            return None

        try:
            resp = self._session.get(
                f"{WU_BASE_URL}/v3/wx/observations/current",
                params={
                    "geocode": f"{info['lat']},{info['lon']}",
                    "format": "json",
                    "units": "m",
                    "language": "en-US",
                    "apiKey": self._api_key,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                obs = {
                    "temperature": data.get("temperature"),
                    "temperatureMax24Hour": data.get("temperatureMax24Hour"),
                    "temperatureMin24Hour": data.get("temperatureMin24Hour"),
                }
                logger.debug(
                    f"[WU] Observations {city}: "
                    f"temp={obs.get('temperature')}°C "
                    f"max24h={obs.get('temperatureMax24Hour')}°C"
                )
                return obs
            return None
        except Exception as e:
            logger.debug(f"[WU] Errore observations {city}: {e}")
            return None

    def _fetch_v3(self, city: str, info: dict) -> dict[str, SourceForecast]:
        """Fetch da API v3: array-based response."""
        try:
            resp = self._session.get(
                f"{WU_BASE_URL}/v3/wx/forecast/daily/5day",
                params={
                    "geocode": f"{info['lat']},{info['lon']}",
                    "format": "json",
                    "units": "m",
                    "language": "en-US",
                    "apiKey": self._api_key,
                },
                timeout=15,
            )

            if resp.status_code == 401:
                logger.warning("[WU] API key non valida (401) — provider disabilitato")
                self._available = False
                return {}

            if resp.status_code == 403:
                logger.warning("[WU] Accesso negato (403) — verificare piano API")
                self._consecutive_errors += 1
                if self._consecutive_errors >= 3:
                    self._available = False
                return {}

            if resp.status_code == 429:
                logger.warning("[WU] Rate limit (429) — skip")
                return {}

            if resp.status_code != 200:
                logger.debug(f"[WU] v3 forecast {resp.status_code} per {city}")
                return {}

            data = resp.json()
            self._consecutive_errors = 0

            # v3 response: arrays indexed by day
            # dayOfWeek[], temperatureMax[], temperatureMin[], validTimeLocal[]
            dates_raw = data.get("validTimeLocal", [])
            temp_max = data.get("temperatureMax", data.get("calendarDayTemperatureMax", []))
            temp_min = data.get("temperatureMin", data.get("calendarDayTemperatureMin", []))

            if not dates_raw or not temp_max:
                logger.debug(f"[WU] v3 response vuota per {city}")
                return {}

            results: dict[str, SourceForecast] = {}
            for i, date_str in enumerate(dates_raw):
                if i >= len(temp_max):
                    break

                # Estrai data YYYY-MM-DD dal timestamp
                date = str(date_str)[:10] if date_str else ""
                if not date or len(date) < 10:
                    continue

                t_max = temp_max[i]
                if t_max is None:
                    continue

                t_max = float(t_max)
                t_min = float(temp_min[i]) if i < len(temp_min) and temp_min[i] is not None else t_max - 5.0

                # WU non ha ensemble — stima incertezza dall'orizzonte
                from datetime import datetime
                try:
                    target = datetime.strptime(date, "%Y-%m-%d")
                    days_ahead = max(0, (target - datetime.now()).days)
                except (ValueError, TypeError):
                    days_ahead = 2
                sigma = max(0.8 + days_ahead * 0.2, (t_max - t_min) / 4.0)

                results[date] = SourceForecast(
                    provider="wunderground",
                    temp=round(t_max, 1),
                    uncertainty=round(sigma, 2),
                    ensemble_temps=[],
                    weight=self.WEIGHT,
                )

            if results:
                first = next(iter(results.values()))
                logger.info(
                    f"[WU] Forecast {city}: {len(results)} giorni, "
                    f"oggi={first.temp:.1f}°C ±{first.uncertainty:.1f} "
                    f"(settlement source)"
                )
            return results

        except Exception as e:
            logger.debug(f"[WU] Errore v3 {city}: {e}")
            return {}

    def _fetch_v1(self, city: str, info: dict) -> dict[str, SourceForecast]:
        """Fallback: API v1 con response object-based."""
        try:
            resp = self._session.get(
                f"{WU_BASE_URL}/v1/geocode/{info['lat']}/{info['lon']}"
                f"/forecast/daily/5day.json",
                params={
                    "apiKey": self._api_key,
                    "units": "m",
                    "language": "en-US",
                },
                timeout=15,
            )

            if resp.status_code != 200:
                logger.debug(f"[WU] v1 forecast {resp.status_code} per {city}")
                return {}

            data = resp.json()
            self._consecutive_errors = 0

            # v1 response: forecasts array
            forecasts = data.get("forecasts", [])
            if not forecasts:
                return {}

            results: dict[str, SourceForecast] = {}
            for fc in forecasts:
                t_max = fc.get("max_temp")
                if t_max is None:
                    continue

                # Data dal campo fcst_valid_local o dow
                date_str = fc.get("fcst_valid_local", "")
                date = str(date_str)[:10]
                if not date or len(date) < 10:
                    continue

                t_max = float(t_max)
                t_min = float(fc.get("min_temp", t_max - 5.0))

                from datetime import datetime
                try:
                    target = datetime.strptime(date, "%Y-%m-%d")
                    days_ahead = max(0, (target - datetime.now()).days)
                except (ValueError, TypeError):
                    days_ahead = 2
                sigma = max(0.8 + days_ahead * 0.2, (t_max - t_min) / 4.0)

                results[date] = SourceForecast(
                    provider="wunderground",
                    temp=round(t_max, 1),
                    uncertainty=round(sigma, 2),
                    ensemble_temps=[],
                    weight=self.WEIGHT,
                )

            if results:
                first = next(iter(results.values()))
                logger.info(
                    f"[WU] v1 Forecast {city}: {len(results)} giorni, "
                    f"oggi={first.temp:.1f}°C"
                )
            return results

        except Exception as e:
            logger.debug(f"[WU] Errore v1 {city}: {e}")
            return {}


# ══════════════════════════════════════════════════════════════
#  WeatherFeed: orchestratore multi-provider con consensus
# ══════════════════════════════════════════════════════════════

@dataclass
class WeatherFeed:
    """
    Feed previsioni meteo multi-sorgente.

    Uso: feed.get_forecast("london") → lista di CityForecast per 7 giorni.
    Ogni CityForecast combina dati da tutti i provider disponibili.
    Cache interna: non chiama le API piu' di 1 volta ogni 30 minuti per citta'.

    Interfaccia backward-compatible con v3.4.
    """

    _cache: dict[str, list[CityForecast]] = field(default_factory=dict)
    _cache_time: dict[str, float] = field(default_factory=dict)
    _prev_cache: dict[str, list[CityForecast]] = field(default_factory=dict)  # v10.8.5: previous forecast for shift detection
    _session: requests.Session = field(default_factory=requests.Session)

    # Provider (inizializzati in __post_init__)
    _openmeteo: OpenMeteoProvider = field(init=False, default=None)
    _wethr: WethrProvider = field(init=False, default=None)
    _nws: NWSProvider = field(init=False, default=None)
    _wunderground: WeatherUndergroundProvider = field(init=False, default=None)

    def __post_init__(self):
        self._openmeteo = OpenMeteoProvider(self._session)

        wethr_key = os.getenv("WETHR_API_KEY", "")
        self._wethr = WethrProvider(self._session, wethr_key)

        self._nws = NWSProvider(self._session)

        wu_key = os.getenv("WUNDERGROUND_API_KEY", "")
        self._wunderground = WeatherUndergroundProvider(self._session, wu_key)

        providers = ["OpenMeteo"]
        if self._wethr.available:
            providers.append("Wethr.net")
        if self._wunderground.available:
            providers.append("WeatherUnderground(settlement)")
        providers.append("NWS")
        logger.info(f"[WEATHER] Provider attivi: {', '.join(providers)}")

    def get_forecast(self, city: str) -> list[CityForecast]:
        """Ottieni previsioni 7 giorni per una citta' (con cache)."""
        now = time.time()
        if city in self._cache and now - self._cache_time.get(city, 0) < CACHE_DURATION:
            return self._cache[city]

        # Fetch da tutti i provider in parallelo (sequenziale qui, async possibile)
        sources_by_date: dict[str, list[SourceForecast]] = {}

        # 1. Open-Meteo (sempre disponibile)
        om_data = self._openmeteo.fetch(city)
        for date, src in om_data.items():
            sources_by_date.setdefault(date, []).append(src)

        # 2. Wethr.net (se API key presente)
        if self._wethr.available:
            try:
                wethr_data = self._wethr.fetch(city)
                for date, src in wethr_data.items():
                    sources_by_date.setdefault(date, []).append(src)
            except Exception as e:
                logger.debug(f"[WEATHER] Wethr fallback: {e}")

        # 3. NWS (solo citta' USA)
        try:
            nws_data = self._nws.fetch(city)
            for date, src in nws_data.items():
                sources_by_date.setdefault(date, []).append(src)
        except Exception as e:
            logger.debug(f"[WEATHER] NWS fallback: {e}")

        # 4. Weather Underground (settlement source — peso 2.0)
        if self._wunderground.available:
            try:
                wu_data = self._wunderground.fetch(city)
                for date, src in wu_data.items():
                    sources_by_date.setdefault(date, []).append(src)
            except Exception as e:
                logger.debug(f"[WEATHER] WU fallback: {e}")

        # Costruisci CityForecast consensus per ogni data
        forecasts: list[CityForecast] = []
        for date in sorted(sources_by_date.keys()):
            sources = sources_by_date[date]
            fc = self._build_consensus(city, date, sources)
            if fc:
                forecasts.append(fc)

        if forecasts:
            self._cache[city] = forecasts
            self._cache_time[city] = now

            first = forecasts[0]
            logger.info(
                f"[WEATHER] Consensus {city}: {len(forecasts)} giorni, "
                f"{first.n_sources} fonti "
                f"({first.source}), "
                f"oggi={first.forecast_temp:.1f}°C ±{first.uncertainty:.1f}"
            )
        else:
            logger.warning(f"[WEATHER] Nessuna previsione per {city}")

        return forecasts

    def get_forecast_for_date(self, city: str, date: str) -> CityForecast | None:
        """Ottieni previsione per una citta' e data specifica.

        Se non trova match esatto, prova ±1 giorno per gestire
        mismatch timezone (es. Seoul UTC+9 → date locali sfasate).
        """
        forecasts = self.get_forecast(city)
        # Exact match first
        exact = next((f for f in forecasts if f.date == date), None)
        if exact:
            return exact
        # Fallback: ±1 day (timezone mismatch)
        from datetime import datetime, timedelta
        try:
            target = datetime.strptime(date, "%Y-%m-%d")
            for delta in [1, -1]:
                adj = (target + timedelta(days=delta)).strftime("%Y-%m-%d")
                match = next((f for f in forecasts if f.date == adj), None)
                if match:
                    logger.debug(
                        f"[WEATHER] Timezone fallback {city}: "
                        f"requested {date}, using {adj}"
                    )
                    return match
        except (ValueError, TypeError):
            pass
        return None

    def detect_city(self, text: str) -> str | None:
        """Rileva la citta' da un testo (es. domanda Polymarket)."""
        t = text.lower()
        for city, info in WEATHER_CITIES.items():
            for kw in info["keywords"]:
                if kw in t:
                    return city
        return None

    def ready(self) -> bool:
        """Almeno una citta' ha previsioni caricate."""
        return len(self._cache) > 0

    def status_summary(self) -> str:
        """Stato del feed per il log."""
        if not self._cache:
            return "Weather: nessuna previsione caricata"
        parts = []
        for city, forecasts in self._cache.items():
            if forecasts:
                today = forecasts[0]
                parts.append(
                    f"{city.upper()}: {today.forecast_temp:.1f}°C "
                    f"(±{today.uncertainty:.1f}) "
                    f"[{today.source}]"
                )
        return " | ".join(parts)

    # ── v11.0: Forecast Error Tracker (Qlib-inspired online calibration) ──

    def record_settlement(self, city: str, date: str, actual_temp_c: float):
        """
        Record actual settlement temperature for a city+date.
        Computes forecast error per source and updates rolling bias/MAE.

        This enables online calibration: if a provider consistently
        over/underpredicts for a city, future forecasts are adjusted.
        """
        if not hasattr(self, '_forecast_errors'):
            self._forecast_errors: dict[str, list[dict]] = {}  # city -> [errors]

        # Look up what we forecasted for this city+date
        forecasts = self._cache.get(city, []) or self._prev_cache.get(city, [])
        fc = next((f for f in forecasts if f.date == date), None)
        if not fc:
            return

        error = fc.forecast_temp - actual_temp_c
        error_record = {
            "date": date,
            "forecast": fc.forecast_temp,
            "actual": actual_temp_c,
            "error": error,
            "source": fc.source,
            "n_sources": fc.n_sources,
            "uncertainty": fc.uncertainty,
        }

        self._forecast_errors.setdefault(city, [])
        self._forecast_errors[city].append(error_record)
        # Keep last 100 per city
        if len(self._forecast_errors[city]) > 100:
            self._forecast_errors[city] = self._forecast_errors[city][-100:]

        logger.info(
            f"[FORECAST-ERROR] {city} {date}: forecast={fc.forecast_temp:.1f}°C "
            f"actual={actual_temp_c:.1f}°C error={error:+.1f}°C "
            f"(source={fc.source})"
        )

    def get_city_bias(self, city: str, window: int = 20) -> dict:
        """
        Get rolling forecast bias and MAE for a city.

        Returns:
          bias: mean signed error (positive = model runs hot)
          mae: mean absolute error
          n: number of observations
          reliability: 1.0 - mae/5.0 (capped at [0, 1])
        """
        if not hasattr(self, '_forecast_errors'):
            return {"bias": 0.0, "mae": 0.0, "n": 0, "reliability": 0.5}

        errors = self._forecast_errors.get(city, [])[-window:]
        if not errors:
            return {"bias": 0.0, "mae": 0.0, "n": 0, "reliability": 0.5}

        error_vals = [e["error"] for e in errors]
        bias = sum(error_vals) / len(error_vals)
        mae = sum(abs(e) for e in error_vals) / len(error_vals)
        reliability = max(0.0, min(1.0, 1.0 - mae / 5.0))

        return {
            "bias": round(bias, 2),
            "mae": round(mae, 2),
            "n": len(errors),
            "reliability": round(reliability, 3),
        }

    def get_bias_adjusted_temp(self, city: str, forecast_temp: float) -> float:
        """
        Adjust forecast temperature by removing estimated bias.
        Only adjusts if we have enough data (n >= 5) and bias is significant.
        """
        stats = self.get_city_bias(city)
        if stats["n"] >= 5 and abs(stats["bias"]) > 0.5:
            adjusted = forecast_temp - stats["bias"]
            logger.debug(
                f"[BIAS-ADJ] {city}: {forecast_temp:.1f}°C → {adjusted:.1f}°C "
                f"(bias={stats['bias']:+.1f}°C from {stats['n']} obs)"
            )
            return adjusted
        return forecast_temp

    def invalidate_cache(self, reason: str = "manual"):
        """v10.8.5: Invalida la cache forecast per forzare un refresh.
        Salva il forecast corrente in _prev_cache per confronto shift."""
        if self._cache:
            self._prev_cache = {city: list(fcs) for city, fcs in self._cache.items()}
        self._cache.clear()
        self._cache_time.clear()
        logger.info(f"[WEATHER] Cache invalidata ({reason}) — {len(self._prev_cache)} citta' salvate per shift detection")

    def get_forecast_shift(self, city: str, date: str) -> float | None:
        """v10.8.5: Confronta forecast corrente vs precedente per una citta+data.
        Ritorna la differenza in °C (positivo = riscaldamento, negativo = raffreddamento).
        None se non c'e' un forecast precedente."""
        prev_forecasts = self._prev_cache.get(city)
        if not prev_forecasts:
            return None
        prev = next((f for f in prev_forecasts if f.date == date), None)
        curr_forecasts = self._cache.get(city)
        if not curr_forecasts:
            return None
        curr = next((f for f in curr_forecasts if f.date == date), None)
        if not prev or not curr:
            return None
        shift = curr.forecast_temp - prev.forecast_temp
        if abs(shift) > 0.3:  # ignora noise sotto 0.3°C
            logger.info(
                f"[WEATHER-SHIFT] {city} {date}: {prev.forecast_temp:.1f}°C → "
                f"{curr.forecast_temp:.1f}°C (shift={shift:+.1f}°C)"
            )
        return shift

    def get_observations(self, city: str) -> dict | None:
        """
        Osservazioni real-time (per mercati same-day).

        Priorita': WU (settlement source) > Wethr.net.
        Utile per sapere la max temperatura GIA' osservata oggi.
        """
        # WU ha priorita' — e' la fonte di settlement
        if self._wunderground.available:
            wu_obs = self._wunderground.fetch_observations(city)
            if wu_obs and wu_obs.get("temperature") is not None:
                return wu_obs

        if not self._wethr.available:
            return None
        return self._wethr.fetch_observations(city)

    # ── Consensus building ────────────────────────────────────

    def _build_consensus(
        self, city: str, date: str, sources: list[SourceForecast]
    ) -> CityForecast | None:
        """Combina previsioni da piu' provider in un singolo CityForecast."""
        if not sources:
            return None

        # Media pesata della temperatura
        total_weight = sum(s.weight for s in sources)
        if total_weight <= 0:
            return None

        avg_temp = sum(s.temp * s.weight for s in sources) / total_weight
        avg_unc = sum(s.uncertainty * s.weight for s in sources) / total_weight

        # Se i provider divergono molto, aumenta l'incertezza
        if len(sources) >= 2:
            temps = [s.temp for s in sources]
            spread = max(temps) - min(temps)
            if spread > avg_unc:
                # Spread tra modelli e' piu' grande dell'incertezza stimata
                # → situazione incerta, aumenta incertezza
                avg_unc = max(avg_unc, spread * 0.5)
                logger.debug(
                    f"[WEATHER] {city} {date}: spread {spread:.1f}°C "
                    f"tra provider — incertezza aumentata a ±{avg_unc:.1f}"
                )

        # Merge ensemble per backward compatibility
        # Include tutti gli ensemble members da tutti i provider
        merged_ensemble: list[float] = []
        for s in sources:
            merged_ensemble.extend(s.ensemble_temps)

        return CityForecast(
            city=city,
            date=date,
            forecast_temp=round(avg_temp, 1),
            ensemble_temps=merged_ensemble,
            uncertainty=round(avg_unc, 2),
            sources=sources,
            updated_at=time.time(),
        )
