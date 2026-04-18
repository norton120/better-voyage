"""Open-Meteo clients.

Two endpoints, one provider:

- **Marine API** — wave, swell, and ocean-current hourly fields
  (https://marine-api.open-meteo.com/v1/marine).
- **Forecast API** — wind at 10 m from the general forecast endpoint
  (https://api.open-meteo.com/v1/forecast).

Both are TTL-cached via `app.clients.cache`. The router never calls
these directly; it reads from the prefetched `ForecastField` (M2).
"""

from __future__ import annotations

from datetime import date, datetime

from app.clients._http import get_json
from app.clients.cache import CacheResult, ForecastCacheStore, cache_or_fetch
from app.config import get_settings

MARINE_VARS: tuple[str, ...] = (
    "wave_height",
    "wave_direction",
    "wave_period",
    "wind_wave_height",
    "wind_wave_direction",
    "wind_wave_period",
    "swell_wave_height",
    "swell_wave_direction",
    "swell_wave_period",
    "ocean_current_velocity",
    "ocean_current_direction",
)

WIND_VARS: tuple[str, ...] = (
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
)

_MARINE_SOURCE = "open_meteo_marine"
_FORECAST_SOURCE = "open_meteo_forecast"


def _to_date(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return d.date().isoformat()
    return d.isoformat()


async def fetch_marine(
    lat: float,
    lon: float,
    start: date | datetime,
    end: date | datetime,
    variables: tuple[str, ...] = MARINE_VARS,
) -> CacheResult:
    """Hourly marine forecast for a single point.

    Grid sampling for a bbox (doc 03 §Sampling & prefetch) is built on
    top of this by the M2 `forecast_prefetching` stage.
    """
    settings = get_settings()
    params: dict[str, str | int | float] = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "hourly": ",".join(variables),
        "start_date": _to_date(start),
        "end_date": _to_date(end),
        "timezone": "GMT",
    }
    url = f"{settings.open_meteo_marine_base_url}/marine"
    return await cache_or_fetch(
        source=_MARINE_SOURCE,
        params={"url": url, **params},
        store=ForecastCacheStore(),
        ttl_s=settings.forecast_cache_ttl_s,
        fetcher=lambda: get_json(url, params),
    )


async def fetch_wind(
    lat: float,
    lon: float,
    start: date | datetime,
    end: date | datetime,
    variables: tuple[str, ...] = WIND_VARS,
) -> CacheResult:
    """Hourly wind at 10 m for a single point (general forecast API)."""
    settings = get_settings()
    params: dict[str, str | int | float] = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "hourly": ",".join(variables),
        "start_date": _to_date(start),
        "end_date": _to_date(end),
        "timezone": "GMT",
    }
    url = f"{settings.open_meteo_forecast_base_url}/forecast"
    return await cache_or_fetch(
        source=_FORECAST_SOURCE,
        params={"url": url, **params},
        store=ForecastCacheStore(),
        ttl_s=settings.forecast_cache_ttl_s,
        fetcher=lambda: get_json(url, params),
    )
