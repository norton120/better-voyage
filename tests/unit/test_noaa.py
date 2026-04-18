"""NOAA CO-OPS client tests."""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.clients import noaa
from tests.fixtures import load_http_fixture


@pytest.mark.asyncio
async def test_fetch_tide_predictions_caches(httpx_mock) -> None:
    payload = load_http_fixture("noaa_tides_annapolis.json")
    httpx_mock.add_response(
        url=re.compile(r"https://api\.tidesandcurrents\.noaa\.gov/api/prod/datagetter.*"),
        json=payload,
    )
    first = await noaa.fetch_tide_predictions(
        station_id="8575512",
        begin=date(2026, 4, 18),
        end=date(2026, 4, 18),
    )
    assert first.stale is False
    assert len(first.body["predictions"]) == 4
    assert first.body["predictions"][0]["type"] == "H"

    # Second call with same args: cache hit, no new HTTP request.
    second = await noaa.fetch_tide_predictions(
        station_id="8575512",
        begin=date(2026, 4, 18),
        end=date(2026, 4, 18),
    )
    assert second.body == first.body
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_list_tide_stations_populates_cache(httpx_mock) -> None:
    payload = load_http_fixture("noaa_stations_list.json")
    httpx_mock.add_response(
        url=re.compile(r"https://api\.tidesandcurrents\.noaa\.gov/mdapi/prod/webapi/stations\.json.*"),
        json=payload,
    )
    stations = await noaa.list_tide_stations()
    ids = {s.id for s in stations}
    assert "8575512" in ids
    assert "8452660" in ids

    # Second call: served from stations_cache, no network.
    again = await noaa.list_tide_stations()
    assert {s.id for s in again} == ids
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_get_station_hits_cache_after_list(httpx_mock) -> None:
    payload = load_http_fixture("noaa_stations_list.json")
    httpx_mock.add_response(
        url=re.compile(r"https://api\.tidesandcurrents\.noaa\.gov/mdapi/prod/webapi/stations\.json.*"),
        json=payload,
    )
    await noaa.list_tide_stations()
    station = await noaa.get_station("8575512")
    assert station is not None
    assert station.name == "Annapolis"
    # Only the list call hit the network.
    assert len(httpx_mock.get_requests()) == 1
