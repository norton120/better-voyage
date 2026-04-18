"""GET /pois tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_bbox_returns_pois(client: AsyncClient) -> None:
    # bbox covering the Chesapeake: Annapolis, Solomons, Deltaville.
    resp = await client.get("/pois?bbox=-77.0,37.0,-76.0,39.5")
    assert resp.status_code == 200
    body = resp.json()
    names = {p["name"] for p in body}
    assert "Annapolis Harbor" in names
    assert "Solomons Island" in names
    assert "Deltaville Anchorage" in names


@pytest.mark.asyncio
async def test_sym_filter(client: AsyncClient) -> None:
    resp = await client.get("/pois?bbox=-77.0,36.0,-76.0,39.5&sym=Anchor")
    assert resp.status_code == 200
    body = resp.json()
    assert all(p["sym"] == "Anchor" for p in body)


@pytest.mark.asyncio
async def test_type_filter_hazard(client: AsyncClient) -> None:
    resp = await client.get("/pois?bbox=-77.0,36.0,-76.0,39.5&type=hazard")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 1
    assert all(p["type"] == "hazard" for p in body)


@pytest.mark.asyncio
async def test_invalid_bbox_400(client: AsyncClient) -> None:
    resp = await client.get("/pois?bbox=not-a-bbox")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_BBOX"


@pytest.mark.asyncio
async def test_bbox_outside_coverage_returns_empty(client: AsyncClient) -> None:
    resp = await client.get("/pois?bbox=-125.0,30.0,-120.0,35.0")
    assert resp.status_code == 200
    assert resp.json() == []
