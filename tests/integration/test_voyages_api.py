"""Voyages API — end-to-end lifecycle + idempotency matrix.

Tests run against a FastAPI app with the full lifespan (registry
initialized, tables created) via asgi-lifespan. The planner stubs are
essentially instant, so most tests can wait <1 s for terminal states.
Idempotency scenarios that need a "live" voyage swap the registry's
runner for a blocking one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "origin": {"lat": 38.9784, "lon": -76.4922, "name": "Annapolis"},
        "destination": {"lat": 36.8467, "lon": -76.2929, "name": "Norfolk"},
        "window": {
            "start_at": "2026-04-20T00:00:00Z",
            "end_at": "2026-04-27T00:00:00Z",
            "tz": "America/New_York",
        },
        "boat_profile_name": "saltbreaker",
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
async def test_post_voyage_runs_to_done(client: AsyncClient) -> None:
    resp = await client.post("/voyages", json=_payload())
    assert resp.status_code == 202
    body = resp.json()
    assert body["id"].startswith("vy_")
    assert body["status"] == "queued"
    assert body["links"]["self"] == f"/voyages/{body['id']}"

    final = await _poll_until_done(client, body["id"])
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
async def test_cancel_terminal_voyage_is_noop(client: AsyncClient) -> None:
    resp = await client.post("/voyages", json=_payload())
    vid = resp.json()["id"]
    await _poll_until_done(client, vid)

    cancel = await client.post(f"/voyages/{vid}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "done"  # unchanged
