"""Open-Meteo client tests with replayed HTTP fixtures."""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.clients import open_meteo
from tests.fixtures import load_http_fixture


@pytest.mark.asyncio
async def test_fetch_marine_parses_and_caches(httpx_mock) -> None:
    payload = load_http_fixture("open_meteo_marine_annapolis.json")
    httpx_mock.add_response(
        url=re.compile(r"https://marine-api\.open-meteo\.com/v1/marine.*"),
        json=payload,
    )

    result = await open_meteo.fetch_marine(
        lat=38.9833,
        lon=-76.4803,
        start=date(2026, 4, 18),
        end=date(2026, 4, 18),
    )
    assert result.stale is False
    assert result.body["hourly"]["wave_height"] == [0.4, 0.5, 0.5]

    # Second call: same params → served from cache, no new HTTP request.
    again = await open_meteo.fetch_marine(
        lat=38.9833,
        lon=-76.4803,
        start=date(2026, 4, 18),
        end=date(2026, 4, 18),
    )
    assert again.body == result.body
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_fetch_wind_uses_forecast_endpoint(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://api\.open-meteo\.com/v1/forecast.*"),
        json={
            "hourly": {
                "time": ["2026-04-18T00:00"],
                "wind_speed_10m": [5.2],
                "wind_direction_10m": [180],
                "wind_gusts_10m": [7.1],
            }
        },
    )
    result = await open_meteo.fetch_wind(
        lat=38.9833,
        lon=-76.4803,
        start=date(2026, 4, 18),
        end=date(2026, 4, 18),
    )
    assert result.body["hourly"]["wind_speed_10m"] == [5.2]
