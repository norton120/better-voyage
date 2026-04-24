"""Per-voyage wallclock cap surfaces as `ROUTE_BUDGET_EXHAUSTED`.

When the total time spent in the routing stage exceeds
`_VOYAGE_WALLCLOCK_BUDGET_S`, every remaining candidate short-circuits
with `route_budget_exhausted` instead of running. If that reason
dominates the skip reasons AND no candidate survived, the planner
raises a distinct error code so the UI can say "too expensive to
compute" rather than the ambiguous "no route exists."
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
            "wind_speed_10m": [22.2] * n_hours,
            "wind_direction_10m": [180.0] * n_hours,
            "wind_gusts_10m": [30.0] * n_hours,
        }
    }


@pytest.mark.asyncio
async def test_voyage_budget_exhausted_surfaces_distinct_error(
    client: AsyncClient, httpx_mock, monkeypatch: pytest.MonkeyPatch
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

    # Budget of 0 s: every candidate short-circuits before running.
    from app.services import planner as planner_mod
    monkeypatch.setattr(planner_mod, "_VOYAGE_WALLCLOCK_BUDGET_S", 0)

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
    vid = resp.json()["id"]

    body: dict | None = None
    for _ in range(500):
        state = await client.get(f"/voyages/{vid}")
        body = state.json()
        if body["status"] in {"done", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.02)
    assert body is not None
    assert body["status"] == "failed", f"final={body}"
    assert body["error"]["code"] == "ROUTE_BUDGET_EXHAUSTED"
