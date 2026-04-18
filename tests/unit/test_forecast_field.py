"""ForecastField prefetch + trilinear interpolation tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from app.services.forecast_field import KMH_TO_KTS, ForecastField


def _marine_body(n_hours: int = 3) -> dict:
    hours = [f"2026-04-18T{h:02d}:00" for h in range(n_hours)]
    return {
        "hourly": {
            "time": hours,
            "wave_height": [0.5] * n_hours,
            "wave_direction": [180.0] * n_hours,
            "wave_period": [3.0] * n_hours,
            "wind_wave_height": [0.4] * n_hours,
            "wind_wave_direction": [180.0] * n_hours,
            "wind_wave_period": [2.8] * n_hours,
            "swell_wave_height": [0.2] * n_hours,
            "swell_wave_direction": [180.0] * n_hours,
            "swell_wave_period": [4.0] * n_hours,
            "ocean_current_velocity": [1.852] * n_hours,  # km/h → 1 kt
            "ocean_current_direction": [90.0] * n_hours,
        }
    }


def _wind_body(n_hours: int = 3) -> dict:
    hours = [f"2026-04-18T{h:02d}:00" for h in range(n_hours)]
    return {
        "hourly": {
            "time": hours,
            "wind_speed_10m": [18.52] * n_hours,  # km/h → 10 kt
            "wind_direction_10m": [225.0] * n_hours,
            "wind_gusts_10m": [27.78] * n_hours,  # km/h → 15 kt
        }
    }


@pytest.mark.asyncio
async def test_prefetch_builds_grid_and_samples(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://marine-api\.open-meteo\.com/.*"),
        json=_marine_body(),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"https://api\.open-meteo\.com/v1/forecast.*"),
        json=_wind_body(),
        is_reusable=True,
    )

    field = ForecastField(grid_res_deg=0.5)
    await field.prefetch(
        bbox=(38.0, -77.0, 39.0, -76.0),
        start=datetime(2026, 4, 18, tzinfo=UTC),
        end=datetime(2026, 4, 18, tzinfo=UTC),
    )

    env = field.at(38.5, -76.5, datetime(2026, 4, 18, 1, 0, tzinfo=UTC))
    assert env is not None
    assert env.wind_speed_kts == pytest.approx(10.0, rel=1e-3)
    assert env.wind_gust_kts == pytest.approx(15.0, rel=1e-3)
    assert env.wave_height_m == pytest.approx(0.5)
    assert env.current_speed_kts == pytest.approx(1.0, rel=1e-3)


@pytest.mark.asyncio
async def test_at_returns_none_outside_bbox(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://marine-api\.open-meteo\.com/.*"),
        json=_marine_body(),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"https://api\.open-meteo\.com/v1/forecast.*"),
        json=_wind_body(),
        is_reusable=True,
    )

    field = ForecastField(grid_res_deg=0.5)
    await field.prefetch(
        bbox=(38.0, -77.0, 39.0, -76.0),
        start=datetime(2026, 4, 18, tzinfo=UTC),
        end=datetime(2026, 4, 18, tzinfo=UTC),
    )

    t = datetime(2026, 4, 18, 1, 0, tzinfo=UTC)
    assert field.at(37.0, -76.5, t) is None  # below lat_min
    assert field.at(38.5, -75.0, t) is None  # above lon_max


@pytest.mark.asyncio
async def test_at_returns_none_outside_time_window(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://marine-api\.open-meteo\.com/.*"),
        json=_marine_body(),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"https://api\.open-meteo\.com/v1/forecast.*"),
        json=_wind_body(),
        is_reusable=True,
    )

    field = ForecastField(grid_res_deg=0.5)
    await field.prefetch(
        bbox=(38.0, -77.0, 39.0, -76.0),
        start=datetime(2026, 4, 18, tzinfo=UTC),
        end=datetime(2026, 4, 18, tzinfo=UTC),
    )

    # Time axis is 00..02 UTC; asking for 05:00 → None
    assert field.at(38.5, -76.5, datetime(2026, 4, 18, 5, 0, tzinfo=UTC)) is None


def test_km_h_to_knots_factor() -> None:
    # 1.852 km/h == 1 kt
    assert pytest.approx(1.0) == 1.852 * KMH_TO_KTS
