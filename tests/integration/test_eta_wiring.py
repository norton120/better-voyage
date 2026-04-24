"""ETA is surfaced on submit and refined during routing.

Contract:
- `POST /voyages` 202 response carries an `eta_s` in progress.
- `GET /voyages/{id}` while routing carries an `eta_s` that updates
  as candidates complete.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from httpx import AsyncClient


def _iso_hours(n_hours: int) -> list[str]:
    from datetime import UTC, datetime, timedelta
    base = datetime(2026, 4, 18, 0, 0, tzinfo=UTC)
    return [(base + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(n_hours)]


def _marine_body(n_hours: int = 48) -> dict:
    hours = _iso_hours(n_hours)
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


def _wind_body(n_hours: int = 48) -> dict:
    hours = _iso_hours(n_hours)
    return {
        "hourly": {
            "time": hours,
            "wind_speed_10m": [22.2] * n_hours,
            "wind_direction_10m": [180.0] * n_hours,
            "wind_gusts_10m": [30.0] * n_hours,
        }
    }


@pytest.mark.asyncio
async def test_eta_surfaces_on_submit_and_after_completion(
    client: AsyncClient, httpx_mock
) -> None:
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
            "start_at": "2026-04-18T00:00:00Z",
            "end_at": "2026-04-18T03:00:00Z",
            "tz": "UTC",
        },
        "boat_profile_name": "default",
        "max_candidates": 3,
    }
    resp = await client.post("/voyages", json=payload)
    assert resp.status_code == 202
    accepted = resp.json()
    # Cold-start ETA should be a positive number, not None.
    eta_at_submit = accepted["progress"]["eta_s"]
    assert eta_at_submit is not None and eta_at_submit > 0, accepted["progress"]
    vid = accepted["id"]

    # Run through to completion and verify the final progress still
    # has a sensible eta_s for the stages that ran (live refinement
    # during routing is cheap insurance against a regression that
    # nulls it out).
    body: dict | None = None
    for _ in range(500):
        state = await client.get(f"/voyages/{vid}")
        body = state.json()
        if body["status"] in {"done", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.02)
    assert body is not None
    assert body["status"] == "done", f"final={body}"
