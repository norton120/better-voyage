"""Voyages API — end-to-end lifecycle + idempotency matrix.

Tests run against a FastAPI app with the full lifespan (registry
initialized, tables created) via asgi-lifespan. The planner stubs are
essentially instant, so most tests can wait <1 s for terminal states.
Idempotency scenarios that need a "live" voyage swap the registry's
runner for a blocking one.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from httpx import AsyncClient


def _open_meteo_marine_body(n_hours: int = 24) -> dict[str, Any]:
    hours = [f"2026-04-18T{h:02d}:00" for h in range(n_hours)]
    return {
        "hourly": {
            "time": hours,
            "wave_height": [0.4] * n_hours,
            "wave_direction": [180.0] * n_hours,
            "wave_period": [3.5] * n_hours,
            "wind_wave_height": [0.3] * n_hours,
            "wind_wave_direction": [180.0] * n_hours,
            "wind_wave_period": [2.8] * n_hours,
            "swell_wave_height": [0.2] * n_hours,
            "swell_wave_direction": [180.0] * n_hours,
            "swell_wave_period": [4.0] * n_hours,
            "ocean_current_velocity": [0.0] * n_hours,
            "ocean_current_direction": [0.0] * n_hours,
        }
    }


def _open_meteo_forecast_body(n_hours: int = 24) -> dict[str, Any]:
    hours = [f"2026-04-18T{h:02d}:00" for h in range(n_hours)]
    return {
        "hourly": {
            "time": hours,
            "wind_speed_10m": [22.2] * n_hours,  # km/h → 12 kt
            "wind_direction_10m": [180.0] * n_hours,
            "wind_gusts_10m": [30.0] * n_hours,
        }
    }


def _register_open_meteo_mocks(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://marine-api\.open-meteo\.com/.*"),
        json=_open_meteo_marine_body(),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"https://api\.open-meteo\.com/v1/forecast.*"),
        json=_open_meteo_forecast_body(),
        is_reusable=True,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    # Short east-bound hop with a narrow departure window so the planner
    # enumerates only a few candidates during tests.
    base: dict[str, Any] = {
        "origin": {"lat": 38.5, "lon": -76.5, "name": "Origin"},
        "destination": {"lat": 38.5, "lon": -76.07, "name": "Destination"},
        "window": {
            "start_at": "2026-04-18T00:00:00Z",
            "end_at": "2026-04-18T02:00:00Z",
            "tz": "UTC",
        },
        "boat_profile_name": "saltbreaker",
        "max_candidates": 2,
    }
    base.update(overrides)
    return base


async def _poll_until_done(client: AsyncClient, vid: str, timeout_s: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/voyages/{vid}")
        body = resp.json()
        if body["status"] in {"done", "failed", "cancelled"}:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"voyage {vid} did not terminate: last={body}")


@pytest.mark.asyncio
async def test_post_voyage_runs_to_done(client: AsyncClient, httpx_mock) -> None:
    _register_open_meteo_mocks(httpx_mock)

    resp = await client.post("/voyages", json=_payload())
    assert resp.status_code == 202
    body = resp.json()
    assert body["id"].startswith("vy_")
    assert body["status"] == "queued"
    assert body["links"]["self"] == f"/voyages/{body['id']}"

    final = await _poll_until_done(client, body["id"], timeout_s=15.0)
    assert final["status"] == "done"
    assert final["error"] is None
    assert final["voyage"]["gpx_available"] is True


@pytest.mark.asyncio
async def test_get_unknown_voyage_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/voyages/vy_doesnotexist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "VOYAGE_NOT_FOUND"


@pytest.mark.asyncio
async def test_dedupe_same_hash_while_live(client: AsyncClient) -> None:
    """Same-inputs-hash submission against a live voyage returns the
    same id — not a fresh job."""
    # Replace the runner with a blocking one so the first voyage stays
    # in a live stage across submission 2.
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_runner(_: str) -> None:
        from app.services.jobs import set_stage

        await set_stage(_, "routing", pct=0.0)
        started.set()
        await release.wait()

    from app.main import app
    app.state.registry._runner = blocking_runner

    first = await client.post("/voyages", json=_payload())
    vid = first.json()["id"]
    await asyncio.wait_for(started.wait(), timeout=2)

    second = await client.post("/voyages", json=_payload())
    assert second.status_code == 202
    assert second.json()["id"] == vid  # dedupe — same id

    release.set()


@pytest.mark.asyncio
async def test_conflicting_hash_while_live_is_409(client: AsyncClient) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocking_runner(_: str) -> None:
        from app.services.jobs import set_stage

        await set_stage(_, "routing", pct=0.0)
        started.set()
        await release.wait()

    from app.main import app
    app.state.registry._runner = blocking_runner

    await client.post("/voyages", json=_payload())
    await asyncio.wait_for(started.wait(), timeout=2)

    different = _payload(destination={"lat": 40.0, "lon": -74.0, "name": "NYC"})
    resp = await client.post("/voyages", json=different)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "VOYAGE_IN_PROGRESS"

    release.set()


@pytest.mark.asyncio
async def test_force_replaces_live_voyage(client: AsyncClient) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocking_runner(_: str) -> None:
        from app.services.jobs import set_stage

        await set_stage(_, "routing", pct=0.0)
        started.set()
        await release.wait()

    from app.main import app
    registry = app.state.registry
    registry._runner = blocking_runner

    first = await client.post("/voyages", json=_payload())
    vid1 = first.json()["id"]
    await asyncio.wait_for(started.wait(), timeout=2)

    # ?force=true replaces even when different hash + live.
    different = _payload(destination={"lat": 40.0, "lon": -74.0, "name": "NYC"})
    resp = await client.post("/voyages?force=true", json=different)
    assert resp.status_code == 202
    vid2 = resp.json()["id"]
    assert vid2 != vid1

    release.set()


@pytest.mark.asyncio
async def test_cancel_returns_cancelled(client: AsyncClient) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocking_runner(_: str) -> None:
        from app.services.jobs import set_stage

        await set_stage(_, "routing", pct=0.0)
        started.set()
        await release.wait()

    from app.main import app
    app.state.registry._runner = blocking_runner

    resp = await client.post("/voyages", json=_payload())
    vid = resp.json()["id"]
    await asyncio.wait_for(started.wait(), timeout=2)

    cancel = await client.post(f"/voyages/{vid}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"

    state = await client.get(f"/voyages/{vid}")
    assert state.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_terminal_voyage_is_noop(client: AsyncClient, httpx_mock) -> None:
    _register_open_meteo_mocks(httpx_mock)

    resp = await client.post("/voyages", json=_payload())
    vid = resp.json()["id"]
    await _poll_until_done(client, vid, timeout_s=15.0)

    cancel = await client.post(f"/voyages/{vid}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "done"  # unchanged
