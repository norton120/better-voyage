"""GET /charts/point — water / depth / land-distance for a single coord.

Issue 01: the UI needs a cheap, consistent check so a skipper's pick
can be refused before a full voyage run. Under `BV_CHART_STORE_MODE=
null` the store treats everything as navigable water, so these tests
mostly assert shape + bounds; the on-land branch is covered by a unit
test that monkeypatches the singleton.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_point_returns_shape_under_null_store(client: AsyncClient) -> None:
    resp = await client.get("/charts/point", params={"lat": 38.9, "lon": -76.5})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "in_water", "depth_m", "distance_to_land_nm", "coverage_loaded",
    }
    # NullChartStore reports no gaps and a permissive world — treat as
    # "coverage loaded" so the UI doesn't render a loading hint forever
    # in dev / test mode.
    assert body["coverage_loaded"] is True
    assert body["in_water"] is True
    # NullChartStore returns math.inf, which FastAPI's jsonable_encoder
    # replaces with null on the wire. Either is fine — anything that
    # isn't a finite positive number means "no land anywhere near."
    d = body["distance_to_land_nm"]
    assert d is None or d > 0.0


@pytest.mark.asyncio
async def test_point_rejects_bad_coords(client: AsyncClient) -> None:
    resp = await client.get("/charts/point", params={"lat": 95.0, "lon": 0.0})
    assert resp.status_code == 422
    resp = await client.get("/charts/point", params={"lat": 0.0, "lon": 200.0})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_point_detects_on_land(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap the singleton with a stub that reports the point is on land."""
    from app.services import charts as charts_svc

    class _OnLandStore:
        async def coverage(self, bbox):
            return charts_svc.ChartCoverage(
                enc_cells=1, osm_extracts=1, gebco_tile=None,
                fetched_at=None, tide_modulated_depth=False, gaps=[],
            )

        def distance_to_land_nm(self, lat, lon):
            return 0.0

        def chart_depth(self, lat, lon):
            return None

    monkeypatch.setattr(charts_svc, "_instance", _OnLandStore())
    try:
        resp = await client.get(
            "/charts/point", params={"lat": 38.33, "lon": -76.45},
        )
    finally:
        charts_svc.reset_chart_store()
    assert resp.status_code == 200
    body = resp.json()
    assert body["in_water"] is False
    assert body["distance_to_land_nm"] == 0.0
    assert body["coverage_loaded"] is True
