"""End-to-end: POST /voyages produces a routed candidate GPX blob.

Exercises the full M2 pipeline: forecast prefetch (mocked upstreams) →
isochrone router → scorer → GPX emission. The voyage row's
`gpx_blob` is a GPX 1.1 document with multiple <rtept> elements.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from httpx import AsyncClient


def _marine_body(n_hours: int = 24) -> dict:
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


def _wind_body(n_hours: int = 24) -> dict:
    hours = [f"2026-04-18T{h:02d}:00" for h in range(n_hours)]
    return {
        "hourly": {
            "time": hours,
            "wind_speed_10m": [22.2] * n_hours,  # km/h -> 12 kt
            "wind_direction_10m": [180.0] * n_hours,
            "wind_gusts_10m": [30.0] * n_hours,
        }
    }


@pytest.mark.asyncio
async def test_pipeline_emits_multi_rtept_gpx(client: AsyncClient, httpx_mock) -> None:
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

    payload = {
        "origin": {"lat": 38.5, "lon": -76.5, "name": "Origin"},
        "destination": {"lat": 38.5, "lon": -76.07, "name": "Destination"},
        "window": {
            # 3-hour window → 4 departure candidates routed in parallel.
            "start_at": "2026-04-18T00:00:00Z",
            "end_at": "2026-04-18T03:00:00Z",
            "tz": "UTC",
        },
        "boat_profile_name": "default",
        "max_candidates": 3,
    }
    resp = await client.post("/voyages", json=payload)
    assert resp.status_code == 202
    vid = resp.json()["id"]

    body: dict | None = None
    for _ in range(500):  # ~10 seconds
        state = await client.get(f"/voyages/{vid}")
        body = state.json()
        if body["status"] in {"done", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.02)
    assert body is not None
    assert body["status"] == "done", f"final={body}"
    assert body["voyage"]["gpx_available"] is True

    # Pull the GPX blob directly from the DB (the /gpx endpoint is M5).
    from app.db import session_scope
    from app.models.voyage import Voyage

    async with session_scope() as session:
        row = await session.get(Voyage, vid)
    assert row is not None
    assert row.gpx_blob is not None
    text = row.gpx_blob.decode()
    # Multi-candidate: one <rte> per surfaced candidate, each with
    # many rtepts (origin, intermediate isochrone waypoints, destination).
    assert text.count("<rte>") >= 2
    assert text.count("<rtept") >= 4
    assert '<bv:score total=' in text
    assert '<bv:candidate rank="1"' in text
    assert 'xmlns:bv="https://better-voyage.app/gpx/1"' in text
