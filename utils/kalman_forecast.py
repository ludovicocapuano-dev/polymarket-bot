"""
Kalman Filter for Weather Forecast Combination v1.0
(Elements of Quantitative Investing, Isichenko)

Replaces simple weighted average of weather providers with optimal
state estimation. Each provider is a noisy observation of the true
temperature. The Kalman filter optimally combines them, weighting
by inverse error variance.

Usage:
    kf = WeatherKalmanFilter()
    kf.update("new_york", provider="openmeteo", value=72.0, timestamp=time.time())
    kf.update("new_york", provider="wu", value=73.5, timestamp=time.time())
    estimate = kf.get_estimate("new_york")
    # estimate.value = optimal combination, estimate.uncertainty = posterior std
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


@dataclass
class KalmanEstimate:
    value: float           # posterior mean (best estimate of true temperature)
    uncertainty: float     # posterior std deviation
    n_observations: int    # number of provider updates incorporated
    last_update: float     # timestamp of last update
    provider_weights: dict[str, float] = field(default_factory=dict)  # effective weight per provider


class SingleStateKalman:
    """
    1D Kalman filter for a single city's temperature.

    State: x = true temperature (scalar)
    Observation: z_i = x + noise_i, noise_i ~ N(0, R_i)

    Process model: x_t = x_{t-1} + process_noise
    Process noise Q models natural temperature change between updates.
    """

    def __init__(self, process_noise: float = 0.5):
        """
        Args:
            process_noise: Q - expected temperature change variance per hour.
                          0.5 deg F is reasonable for hourly temperature variation.
        """
        self.Q = process_noise  # process noise variance

        # State
        self.x: Optional[float] = None  # state estimate (temperature)
        self.P: float = 25.0  # state covariance (initial high uncertainty: +/-5 deg F)
        self.last_time: float = 0.0
        self.n_obs: int = 0
        self._provider_contributions: dict[str, float] = {}

    def predict(self, dt_hours: float):
        """Prediction step: propagate state forward in time."""
        if self.x is None:
            return
        # State doesn't change (no deterministic drift model)
        # But uncertainty grows with time
        self.P += self.Q * dt_hours

    def update(self, z: float, R: float, provider: str = "unknown"):
        """
        Update step: incorporate a new observation.

        Args:
            z: observed temperature from provider
            R: observation noise variance (provider-specific)
            provider: name of the weather provider
        """
        if self.x is None:
            # First observation: initialize state
            self.x = z
            self.P = R
            self.n_obs = 1
            self._provider_contributions[provider] = 1.0
            return

        # Kalman gain
        K = self.P / (self.P + R)

        # Update state
        innovation = z - self.x
        self.x = self.x + K * innovation

        # Update covariance (simplified scalar form, equivalent to Joseph form for 1D)
        self.P = (1 - K) * self.P

        self.n_obs += 1
        self._provider_contributions[provider] = K  # effective weight

        logger.debug(
            f"[KALMAN] provider={provider} obs={z:.1f} gain={K:.3f} "
            f"estimate={self.x:.2f}+/-{self.P**0.5:.2f}"
        )

    @property
    def estimate(self) -> Optional[tuple[float, float]]:
        """Returns (mean, std) or None."""
        if self.x is None:
            return None
        return (self.x, self.P ** 0.5)


# Provider-specific observation noise variances (deg F squared)
# Calibrated from historical forecast errors
PROVIDER_NOISE = {
    "weatherunderground": 2.0,   # WU: +/-1.4 deg F typical error, BUT settlement source (weight via low R)
    "wu": 2.0,
    "openmeteo": 4.0,            # OpenMeteo: +/-2 deg F typical error
    "openweathermap": 5.0,       # OWM: +/-2.2 deg F
    "visualcrossing": 4.5,       # VC: +/-2.1 deg F
    "weatherapi": 5.0,           # WeatherAPI: +/-2.2 deg F
    "tomorrow": 4.0,             # Tomorrow.io: +/-2 deg F
    "default": 6.0,              # Unknown provider: conservative
}


class WeatherKalmanFilter:
    """
    Multi-city Kalman filter manager.
    Maintains one Kalman filter per city, combines multiple provider observations.
    """

    def __init__(self, process_noise: float = 0.5):
        self._filters: dict[str, SingleStateKalman] = {}
        self._process_noise = process_noise

    def _get_filter(self, city: str) -> SingleStateKalman:
        city_key = city.lower().strip()
        if city_key not in self._filters:
            self._filters[city_key] = SingleStateKalman(
                process_noise=self._process_noise
            )
        return self._filters[city_key]

    def update(self, city: str, provider: str, value: float,
               timestamp: float = 0.0, unit: str = "F"):
        """
        Incorporate a new forecast observation.

        Args:
            city: city name
            provider: weather provider name
            value: forecasted temperature
            timestamp: observation time (for time-decay)
            unit: temperature unit (F or C)
        """
        kf = self._get_filter(city)

        # Time-based prediction step
        now = timestamp or time.time()
        if kf.last_time > 0:
            dt_hours = (now - kf.last_time) / 3600.0
            if dt_hours > 0:
                kf.predict(dt_hours)
        kf.last_time = now

        # Get provider-specific noise variance
        provider_key = provider.lower().strip()
        R = PROVIDER_NOISE.get(provider_key, PROVIDER_NOISE["default"])

        # Update
        kf.update(value, R, provider=provider_key)

    def get_estimate(self, city: str) -> Optional[KalmanEstimate]:
        """Get current best estimate for a city."""
        kf = self._get_filter(city)
        est = kf.estimate
        if est is None:
            return None

        return KalmanEstimate(
            value=est[0],
            uncertainty=est[1],
            n_observations=kf.n_obs,
            last_update=kf.last_time,
            provider_weights=dict(kf._provider_contributions),
        )

    def reset(self, city: Optional[str] = None):
        """Reset filter(s) for new forecast cycle."""
        if city:
            city_key = city.lower().strip()
            if city_key in self._filters:
                del self._filters[city_key]
        else:
            self._filters.clear()

    def batch_update(self, city: str, observations: list[tuple[str, float]],
                     timestamp: float = 0.0):
        """
        Update with multiple provider observations at once.

        Args:
            city: city name
            observations: [(provider_name, temperature_value), ...]
            timestamp: common timestamp
        """
        for provider, value in observations:
            self.update(city, provider, value, timestamp)

    @property
    def stats(self) -> dict:
        result = {}
        for city, kf in self._filters.items():
            est = kf.estimate
            if est:
                result[city] = {
                    "estimate": round(est[0], 2),
                    "uncertainty": round(est[1], 2),
                    "n_obs": kf.n_obs,
                }
        return result
