"""
Warming/Cooling Day Classifier (v12.10.9)
==========================================
Pre-dawn classifier: will today be warmer or cooler than yesterday?

Run at 4-6 AM UTC for each city. Uses rule-based scoring with:
- Pressure changes (3h, 12h)
- Wind direction and speed
- Cloud cover
- Yesterday's temperature delta
- Season/month

Output: "warming", "slight_warming", "stable", "slight_cooling", "cooling" + confidence

From the Shanghai weather trader article: "Most accurate in winter (cold air signals clear),
least accurate in autumn (warm/cold tug-of-war)."
"""

import logging
import math
import requests
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Station coordinates for current conditions
CITY_STATIONS = {
    "nyc": {"lat": 40.7789, "lon": -73.9692, "tz": -5, "code": "KNYC"},
    "new york city": {"lat": 40.7789, "lon": -73.9692, "tz": -5, "code": "KNYC"},
    "london": {"lat": 51.4700, "lon": -0.4543, "tz": 0, "code": "EGLL"},
    "chicago": {"lat": 41.9742, "lon": -87.9073, "tz": -6, "code": "KORD"},
    "seattle": {"lat": 47.4502, "lon": -122.3088, "tz": -8, "code": "KSEA"},
    "atlanta": {"lat": 33.6407, "lon": -84.4277, "tz": -5, "code": "KATL"},
    "miami": {"lat": 25.7959, "lon": -80.2870, "tz": -5, "code": "KMIA"},
    "dallas": {"lat": 32.8998, "lon": -97.0403, "tz": -6, "code": "KDFW"},
    "toronto": {"lat": 43.6777, "lon": -79.6248, "tz": -5, "code": "CYYZ"},
    "seoul": {"lat": 37.4602, "lon": 126.4407, "tz": 9, "code": "RKSI"},
    "tokyo": {"lat": 35.5494, "lon": 139.7798, "tz": 9, "code": "RJTT"},
    "paris": {"lat": 49.0097, "lon": 2.5479, "tz": 1, "code": "LFPG"},
    "buenos aires": {"lat": -34.8222, "lon": -58.5358, "tz": -3, "code": "SAEZ"},
    "ankara": {"lat": 40.1281, "lon": 32.9951, "tz": 3, "code": "LTAC"},
    "wellington": {"lat": -41.3272, "lon": 174.8053, "tz": 12, "code": "NZWN"},
    "sao paulo": {"lat": -23.4356, "lon": -46.4731, "tz": -3, "code": "SBGR"},
    "denver": {"lat": 39.8561, "lon": -104.6737, "tz": -7, "code": "KDEN"},
    "los angeles": {"lat": 33.9425, "lon": -118.4081, "tz": -8, "code": "KLAX"},
    "houston": {"lat": 29.9902, "lon": -95.3368, "tz": -6, "code": "KIAH"},
    "phoenix": {"lat": 33.4373, "lon": -112.0078, "tz": -7, "code": "KPHX"},
    "munich": {"lat": 48.3537, "lon": 11.7750, "tz": 1, "code": "EDDM"},
    "sydney": {"lat": -33.9399, "lon": 151.1753, "tz": 10, "code": "YSSY"},
    "san francisco": {"lat": 37.6213, "lon": -122.3790, "tz": -8, "code": "KSFO"},
}


def get_station_info(city: str) -> Optional[dict]:
    """Get station info for a city."""
    return CITY_STATIONS.get(city.lower())


def fetch_current_conditions(city: str) -> Optional[dict]:
    """Fetch current weather conditions from Open-Meteo at station coordinates."""
    station = get_station_info(city)
    if not station:
        return None

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": station["lat"],
                "longitude": station["lon"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,"
                           "wind_direction_10m,cloud_cover,surface_pressure",
                "timezone": "UTC",
            },
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json().get("current", {})
            return {
                "temperature": data.get("temperature_2m"),
                "humidity": data.get("relative_humidity_2m"),
                "wind_speed": data.get("wind_speed_10m"),
                "wind_direction": data.get("wind_direction_10m"),
                "cloud_cover": data.get("cloud_cover"),
                "pressure": data.get("surface_pressure"),
                "station": station["code"],
                "city": city,
            }
    except Exception as e:
        logger.debug(f"[WARMING] Fetch error for {city}: {e}")
    return None


def classify_warming_day(city: str, yesterday_high: float = None,
                         conditions: dict = None) -> Tuple[str, float]:
    """
    Pre-dawn classifier: will today be warmer or cooler than yesterday?

    Returns: (classification, confidence)
    - classification: "warming", "slight_warming", "stable", "slight_cooling", "cooling"
    - confidence: 0.0 to 1.0
    """
    if conditions is None:
        conditions = fetch_current_conditions(city)
    if conditions is None:
        return ("stable", 0.30)

    score = 0.0  # positive = warming, negative = cooling
    factors = 0

    # 1. Pressure change (if available) — falling pressure = warming, rising = cooling
    pressure = conditions.get("pressure")
    if pressure:
        # Low pressure (<1010) tends to bring warm air, high (>1020) cold air
        if pressure < 1005:
            score += 1.5
        elif pressure < 1010:
            score += 0.5
        elif pressure > 1025:
            score -= 1.5
        elif pressure > 1020:
            score -= 0.5
        factors += 1

    # 2. Wind direction — south/southwest = warming, north/northeast = cooling
    wind_dir = conditions.get("wind_direction")
    if wind_dir is not None:
        # Southern hemisphere cities have reversed wind patterns
        station = get_station_info(city)
        southern = station and station["lat"] < 0

        if southern:
            # Southern hemisphere: north wind = warm, south = cold
            if 315 <= wind_dir or wind_dir <= 45:  # North
                score += 1.0
            elif 135 <= wind_dir <= 225:  # South
                score -= 1.0
        else:
            # Northern hemisphere: south wind = warm, north = cold
            if 135 <= wind_dir <= 225:  # South
                score += 1.0
            elif 315 <= wind_dir or wind_dir <= 45:  # North
                score -= 1.0
        factors += 1

    # 3. Wind speed — strong wind = more extreme change
    wind_speed = conditions.get("wind_speed")
    if wind_speed is not None:
        if wind_speed > 30:
            score *= 1.3  # amplify whatever direction
        elif wind_speed > 20:
            score *= 1.1
        factors += 1

    # 4. Cloud cover — clear skies = more warming potential (daytime), clouds = stable
    cloud_cover = conditions.get("cloud_cover")
    if cloud_cover is not None:
        if cloud_cover < 20:
            score += 0.5  # clear sky = sun heats
        elif cloud_cover > 80:
            score -= 0.3  # thick clouds limit warming
        factors += 1

    # 5. Season — spring/fall = more volatile, winter/summer = more predictable
    month = datetime.now(timezone.utc).month
    if month in (12, 1, 2):
        season_confidence = 0.75  # winter: clear signals
    elif month in (6, 7, 8):
        season_confidence = 0.70  # summer: fairly stable
    elif month in (3, 4, 5):
        season_confidence = 0.55  # spring: volatile
    else:
        season_confidence = 0.50  # autumn: least predictable

    # 6. Morning temperature vs yesterday
    current_temp = conditions.get("temperature")
    if current_temp is not None and yesterday_high is not None:
        delta = current_temp - (yesterday_high - 5)  # morning is ~5°C below high
        if delta > 2:
            score += 0.8  # already warmer than expected
        elif delta < -2:
            score -= 0.8

    # Classify
    if factors == 0:
        return ("stable", 0.30)

    confidence = min(0.90, season_confidence * (0.5 + abs(score) / 5))

    if score >= 2.0:
        return ("warming", confidence)
    elif score >= 0.5:
        return ("slight_warming", confidence)
    elif score <= -2.0:
        return ("cooling", confidence)
    elif score <= -0.5:
        return ("slight_cooling", confidence)
    else:
        return ("stable", confidence * 0.8)


def classifier_agrees_with_forecast(classification: str, confidence: float,
                                     forecast_side: str) -> bool:
    """
    Check if the warming/cooling classifier agrees with the proposed trade.

    forecast_side: "YES" (betting temp will be in range) or "NO" (betting it won't)
    For BUY_NO on high ranges: cooling/stable confirms
    For BUY_YES on low ranges: cooling confirms
    For BUY_NO on low ranges: warming confirms
    """
    if confidence < 0.45:
        return True  # low confidence = don't block

    if forecast_side == "NO":
        # BUY_NO = we think price won't reach the range (usually high temps)
        # Cooling/stable supports this
        return classification in ("cooling", "slight_cooling", "stable")
    else:
        # BUY_YES = we think it WILL be in range
        # Any classification can support depending on the range
        return True  # don't block YES bets based on warming alone


def kalman_correct(forecast_high: float, current_temp: float,
                   hour_local: int, cloud_cover: float = 50,
                   wind_speed: float = 10) -> float:
    """
    Real-time Kalman correction of forecast using measured temperature.

    Blends forecast with extrapolated actual temperature.
    Weight shifts from forecast-heavy (morning) to actual-heavy (afternoon).

    forecast_high: original forecast high temperature
    current_temp: current measured temperature
    hour_local: local hour (0-23)
    cloud_cover: percentage (0-100)
    wind_speed: km/h

    Returns: corrected high temperature estimate
    """
    # Kalman gain: increases through the day
    if hour_local < 6:
        kalman_gain = 0.10  # very early, trust forecast
    elif hour_local < 8:
        kalman_gain = 0.20
    elif hour_local < 10:
        kalman_gain = 0.40
    elif hour_local < 12:
        kalman_gain = 0.60
    elif hour_local < 13:
        kalman_gain = 0.72
    elif hour_local < 14:
        kalman_gain = 0.85
    else:
        # After 2 PM: the high is likely already reached
        return current_temp  # use measured directly

    # Extrapolate: how much more can temperature rise?
    # Historical average: morning temp + typical rise for this hour
    typical_remaining_rise = {
        6: 5.0, 7: 4.5, 8: 3.5, 9: 2.5, 10: 1.5,
        11: 0.8, 12: 0.3, 13: 0.1,
    }
    remaining = typical_remaining_rise.get(hour_local, 0.5)

    # Cloud discount
    if cloud_cover > 70:
        remaining *= 0.85
    # Wind discount
    if wind_speed > 20:
        remaining *= 0.90

    extrapolated_high = current_temp + remaining

    # Blend forecast with extrapolation using Kalman gain
    corrected = (1 - kalman_gain) * forecast_high + kalman_gain * extrapolated_high

    return corrected
