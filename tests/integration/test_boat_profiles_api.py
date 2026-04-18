"""Boat profile CRUD tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_default_profile_seeded_on_startup(client: AsyncClient) -> None:
    resp = await client.get("/boat_profiles/default")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "default"
    assert body["polar_path"].endswith("cruiser_40ft_moderate.pol")
    assert body["draft_m"] > 0


@pytest.mark.asyncio
async def test_list_includes_default(client: AsyncClient) -> None:
    resp = await client.get("/boat_profiles")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert "default" in names


@pytest.mark.asyncio
async def test_put_then_get_roundtrip(client: AsyncClient) -> None:
    body = {
        "polar_path": "app/data/polars/cruiser_40ft_moderate.pol",
        "draft_m": 2.1,
        "beam_m": 4.0,
        "max_wind_kts": 35.0,
        "max_seas_m": 3.0,
        "min_depth_m": 0.6,
        "night_sailing_ok": False,
        "motor_available": True,
        "motor_min_wind_kts": 6.0,
    }
    put = await client.put("/boat_profiles/saltbreaker", json=body)
    assert put.status_code == 200
    assert put.json()["name"] == "saltbreaker"
    assert put.json()["night_sailing_ok"] is False

    got = await client.get("/boat_profiles/saltbreaker")
    assert got.status_code == 200
    assert got.json()["draft_m"] == 2.1


@pytest.mark.asyncio
async def test_delete_profile(client: AsyncClient) -> None:
    body = {"polar_path": "x", "draft_m": 1.5, "beam_m": 3.0}
    await client.put("/boat_profiles/ephemeral", json=body)
    resp = await client.delete("/boat_profiles/ephemeral")
    assert resp.status_code == 204
    missing = await client.get("/boat_profiles/ephemeral")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "BOAT_PROFILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_post_voyage_with_unknown_profile_returns_404(client: AsyncClient) -> None:
    payload = {
        "origin": {"lat": 38.5, "lon": -76.5, "name": "A"},
        "destination": {"lat": 38.5, "lon": -76.07, "name": "B"},
        "window": {
            "start_at": "2026-04-18T00:00:00Z",
            "end_at": "2026-04-18T01:00:00Z",
            "tz": "UTC",
        },
        "boat_profile_name": "does-not-exist",
    }
    resp = await client.post("/voyages", json=payload)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "BOAT_PROFILE_NOT_FOUND"
