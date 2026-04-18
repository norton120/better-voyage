"""GET /voyages/{id}/gpx tests."""

from __future__ import annotations

import asyncio
import re

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_gpx_before_done_is_not_ready(client: AsyncClient) -> None:
    # A fresh submission is in queued/live state; the blob isn't ready.
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_runner(voyage_id: str) -> None:
        from app.services.jobs import set_stage

        await set_stage(voyage_id, "routing", pct=0.0)
        started.set()
        await release.wait()

    from app.main import app
    app.state.registry._runner = blocking_runner

    resp = await client.post(
        "/voyages",
        json={
            "origin": {"lat": 38.5, "lon": -76.5},
            "destination": {"lat": 38.5, "lon": -76.1},
            "window": {
                "start_at": "2026-04-18T00:00:00Z",
                "end_at": "2026-04-18T01:00:00Z",
                "tz": "UTC",
            },
            "boat_profile_name": "default",
        },
    )
    vid = resp.json()["id"]
    await asyncio.wait_for(started.wait(), timeout=2)

    gpx = await client.get(f"/voyages/{vid}/gpx")
    assert gpx.status_code == 404
    assert gpx.json()["detail"]["code"] == "VOYAGE_NOT_READY"

    release.set()


@pytest.mark.asyncio
async def test_gpx_after_done_returns_xml(client: AsyncClient, httpx_mock) -> None:
    n = 24
    hours = [f"2026-04-18T{h:02d}:00" for h in range(n)]
    httpx_mock.add_response(
        url=re.compile(r"https://marine-api\.open-meteo\.com/.*"),
        json={
            "hourly": {
                "time": hours,
                "wave_height": [0.4] * n,
                "wave_direction": [180.0] * n,
                "wave_period": [3.5] * n,
                "wind_wave_height": [0.3] * n,
                "wind_wave_direction": [180.0] * n,
                "wind_wave_period": [2.8] * n,
                "swell_wave_height": [0.2] * n,
                "swell_wave_direction": [180.0] * n,
                "swell_wave_period": [4.0] * n,
                "ocean_current_velocity": [0.0] * n,
                "ocean_current_direction": [0.0] * n,
            }
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"https://api\.open-meteo\.com/v1/forecast.*"),
        json={
            "hourly": {
                "time": hours,
                "wind_speed_10m": [22.2] * n,
                "wind_direction_10m": [180.0] * n,
                "wind_gusts_10m": [30.0] * n,
            }
        },
        is_reusable=True,
    )

    post = await client.post(
        "/voyages",
        json={
            "origin": {"lat": 38.5, "lon": -76.5},
            "destination": {"lat": 38.5, "lon": -76.07},
            "window": {
                "start_at": "2026-04-18T00:00:00Z",
                "end_at": "2026-04-18T02:00:00Z",
                "tz": "UTC",
            },
            "boat_profile_name": "default",
            "max_candidates": 2,
        },
    )
    vid = post.json()["id"]

    for _ in range(500):
        s = await client.get(f"/voyages/{vid}")
        if s.json()["status"] in {"done", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.02)

    gpx = await client.get(f"/voyages/{vid}/gpx")
    assert gpx.status_code == 200
    assert gpx.headers["content-type"].startswith("application/gpx+xml")
    assert f'filename="voyage-{vid}.gpx"' in gpx.headers["content-disposition"]
    text = gpx.text
    assert text.startswith("<?xml")
    assert "<rte>" in text

    # ?candidate=1 filters to rank 1's primary (+ escape hatches, if any).
    single = await client.get(f"/voyages/{vid}/gpx?candidate=1")
    assert single.status_code == 200
    assert f'filename="voyage-{vid}-candidate-1.gpx"' in single.headers["content-disposition"]
    # Rank-1 candidate should still be present in the filtered body.
    assert 'rank="1"' in single.text
    # Rank-2 primary must be dropped.
    assert 'rank="2"' not in single.text

    # Out-of-range rank → 404 CANDIDATE_NOT_FOUND.
    missing = await client.get(f"/voyages/{vid}/gpx?candidate=99")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "CANDIDATE_NOT_FOUND"


@pytest.mark.asyncio
async def test_gpx_is_well_formed_and_structurally_valid(
    client: AsyncClient, httpx_mock
) -> None:
    """The emitted GPX must parse, declare GPX 1.1 + the bv: namespace,
    and every <rtept> must carry lat/lon/time. Full XSD validation
    lands with the M5 gpxpy rewrite; this is the interim guardrail."""
    import xml.etree.ElementTree as ET

    n = 24
    hours = [f"2026-04-18T{h:02d}:00" for h in range(n)]
    httpx_mock.add_response(
        url=re.compile(r"https://marine-api\.open-meteo\.com/.*"),
        json={
            "hourly": {
                "time": hours,
                "wave_height": [0.4] * n,
                "wave_direction": [180.0] * n,
                "wave_period": [3.5] * n,
                "wind_wave_height": [0.3] * n,
                "wind_wave_direction": [180.0] * n,
                "wind_wave_period": [2.8] * n,
                "swell_wave_height": [0.2] * n,
                "swell_wave_direction": [180.0] * n,
                "swell_wave_period": [4.0] * n,
                "ocean_current_velocity": [0.0] * n,
                "ocean_current_direction": [0.0] * n,
            }
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"https://api\.open-meteo\.com/v1/forecast.*"),
        json={
            "hourly": {
                "time": hours,
                "wind_speed_10m": [22.2] * n,
                "wind_direction_10m": [180.0] * n,
                "wind_gusts_10m": [30.0] * n,
            }
        },
        is_reusable=True,
    )

    post = await client.post(
        "/voyages",
        json={
            "origin": {"lat": 38.5, "lon": -76.5},
            "destination": {"lat": 38.5, "lon": -76.07},
            "window": {
                "start_at": "2026-04-18T00:00:00Z",
                "end_at": "2026-04-18T01:00:00Z",
                "tz": "UTC",
            },
            "boat_profile_name": "default",
            "max_candidates": 2,
        },
    )
    vid = post.json()["id"]
    for _ in range(500):
        s = await client.get(f"/voyages/{vid}")
        if s.json()["status"] in {"done", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.02)

    gpx = await client.get(f"/voyages/{vid}/gpx")
    root = ET.fromstring(gpx.content)
    ns_gpx = "http://www.topografix.com/GPX/1/1"
    ns_bv = "https://better-voyage.app/gpx/1"

    assert root.tag == f"{{{ns_gpx}}}gpx"
    assert root.get("version") == "1.1"

    rtes = root.findall(f"{{{ns_gpx}}}rte")
    assert rtes, "expected at least one <rte>"

    # Candidate primaries appear in strictly ascending rank order —
    # escape-hatch routes (if any) follow their parent candidate
    # immediately. Plan/09 §Deterministic element order.
    ranks_seen: list[int] = []
    for rte in rtes:
        cand = rte.find(f"{{{ns_gpx}}}extensions/{{{ns_bv}}}candidate")
        if cand is not None:
            ranks_seen.append(int(cand.get("rank", "0")))
    assert ranks_seen == sorted(ranks_seen)

    # Every rtept carries lat, lon, and a <time>.
    for rte in rtes:
        for pt in rte.findall(f"{{{ns_gpx}}}rtept"):
            assert pt.get("lat") is not None
            assert pt.get("lon") is not None
            assert pt.find(f"{{{ns_gpx}}}time") is not None
