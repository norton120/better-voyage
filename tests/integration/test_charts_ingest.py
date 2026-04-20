"""Real-mode ChartStore end-to-end.

Everywhere else the suite runs with `BV_CHART_STORE_MODE=null` so
tests don't touch upstreams. This module flips to `real` and seeds
synthetic inputs on disk so `ChartStore.ensure_coverage` runs the
full fetch → preprocess → load path without the network:

- Monkeypatch `fetch_enc_cells` to drop a ready-made preprocessed
  GeoJSON on disk (skipping the ENC preprocessor, which needs a real
  S-57 file).
- Monkeypatch `fetch_osm_extract` to return None (no OSM needed when
  ENC covers the bbox).
- Write a small synthetic GEBCO netCDF and point `BV_GEBCO_PATH` at it.

Coverage: the voyage runs, the coverage block surfaces `enc_cells` /
`gebco_tile` / `fetched_at`, navaids are emitted in the GPX, and a
bbox outside the seeded coverage fails with `CHARTS_NOT_AVAILABLE`.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from httpx import AsyncClient

from app.services import charts as charts_module
from app.services import charts_fetch as charts_fetch_module
from app.services.charts import reset_chart_store
from app.services.charts_fetch import EncCellFetchResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def synthetic_gebco(tmp_path: Path) -> Path:
    """Deep-water GEBCO slice covering a Chesapeake-ish bbox."""
    lats = np.linspace(37.5, 39.5, 21)
    lons = np.linspace(-77.0, -75.5, 16)
    elev = np.full((lats.size, lons.size), -50.0, dtype=np.float64)
    ds = xr.Dataset(
        {"elevation": (("lat", "lon"), elev)},
        coords={"lat": lats, "lon": lons},
    )
    path = tmp_path / "gebco_2024_sub_ice_topo.nc"
    ds.to_netcdf(path)
    ds.close()
    return path


@pytest.fixture()
def charts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "charts"
    (d / "enc").mkdir(parents=True)
    (d / "osm").mkdir(parents=True)
    return d


@pytest.fixture()
def seeded_enc_cell(charts_dir: Path) -> EncCellFetchResult:
    """Drop a pre-preprocessed GeoJSON covering the voyage bbox on disk.

    ChartStore picks up the `.preprocessed.geojson` without invoking
    the ENC preprocessor (no real S-57 parser needed). The bbox it
    derives from the file covers the test's (37.5, -77.0, 39.5, -75.5)
    request envelope.
    """
    cell_id = "USSYN01M"
    cell_dir = charts_dir / "enc" / cell_id
    cell_dir.mkdir(parents=True)
    # The preprocessor would land `.preprocessed.geojson` alongside the
    # `.000`; ChartStore derives the pair via `.with_suffix`.
    s57_path = cell_dir / f"{cell_id}.000"
    s57_path.write_bytes(b"")  # presence only; preprocessor is skipped
    geojson_path = s57_path.with_suffix(".preprocessed.geojson")
    feature_collection = {
        "type": "FeatureCollection",
        "bbox": [-77.0, 37.5, -75.5, 39.5],
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [-76.42, 38.70],
                },
                "properties": {
                    "bv:layer": "navaid",
                    "sym": "Buoy, Red",
                    "name": "R '2'",
                    "desc": "Red lateral buoy; mid-route waypoint.",
                },
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [-76.32, 38.88],
                },
                "properties": {
                    "bv:layer": "navaid",
                    "sym": "Beacon, Green",
                    "name": "G '1'",
                    "desc": "Green beacon; destination approach.",
                },
            },
        ],
    }
    with geojson_path.open("w", encoding="utf-8") as f:
        json.dump(feature_collection, f)
    return EncCellFetchResult(
        cell_id=cell_id,
        s57_path=s57_path,
        fetched_at=datetime.now(UTC),
        bytes_downloaded=0,
    )


@pytest.fixture()
def real_chart_store(
    monkeypatch: pytest.MonkeyPatch,
    charts_dir: Path,
    synthetic_gebco: Path,
    seeded_enc_cell: EncCellFetchResult,
) -> None:
    """Flip the process into real ChartStore mode with synthetic upstreams."""
    monkeypatch.setenv("BV_CHART_STORE_MODE", "real")
    monkeypatch.setenv("BV_CHARTS_DIR", str(charts_dir))
    monkeypatch.setenv("BV_GEBCO_PATH", str(synthetic_gebco))
    # `get_settings` is lru_cached — bust it.
    from app.config import get_settings

    get_settings.cache_clear()
    reset_chart_store()

    async def _fake_fetch_enc_cells(bbox, out_dir, *, client=None):
        return [seeded_enc_cell]

    async def _fake_fetch_osm_extract(bbox, out_dir, *, client=None):
        return None

    monkeypatch.setattr(charts_module, "fetch_enc_cells", _fake_fetch_enc_cells)
    monkeypatch.setattr(charts_module, "fetch_osm_extract", _fake_fetch_osm_extract)
    monkeypatch.setattr(charts_fetch_module, "fetch_enc_cells", _fake_fetch_enc_cells)
    monkeypatch.setattr(
        charts_fetch_module, "fetch_osm_extract", _fake_fetch_osm_extract
    )
    yield
    reset_chart_store()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers (forecast mocks identical to test_end_to_end.py)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_chart_store_runs_end_to_end(
    client: AsyncClient, httpx_mock, real_chart_store
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
        "destination": {"lat": 38.9, "lon": -76.3, "name": "Destination"},
        "window": {
            "start_at": "2026-04-18T00:00:00Z",
            "end_at": "2026-04-18T03:00:00Z",
            "tz": "UTC",
        },
        "boat_profile_name": "default",
        "max_candidates": 2,
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
    assert body["status"] == "done", f"final={body}"

    from app.db import session_scope
    from app.models.voyage import Voyage

    async with session_scope() as session:
        row = await session.get(Voyage, vid)
    assert row is not None and row.gpx_blob is not None
    gpx = row.gpx_blob.decode()

    # coverage block now carries the three source-summary attributes.
    coverage = json.loads(row.coverage_json)
    assert coverage["charts"]["enc_cells"] == 1
    assert coverage["charts"]["osm_extracts"] == 0
    assert coverage["charts"]["gebco_tile"] == "gebco_2024_sub_ice_topo"
    assert coverage["charts"]["tide_modulated_depth"] is False
    assert coverage["charts"]["fetched_at"] is not None

    # GPX carries bv:coverage with the same attributes.
    assert 'encCells="1"' in gpx
    assert 'gebcoTile="gebco_2024_sub_ice_topo"' in gpx
    # Navaids show up as top-level <wpt>s with the preseeded sym names.
    assert "Buoy, Red" in gpx
    assert "Beacon, Green" in gpx


@pytest.mark.asyncio
async def test_bbox_outside_coverage_fails_charts_not_available(
    client: AsyncClient,
    real_chart_store,
) -> None:
    """Origin/dest outside the seeded cell's panel bbox → CHARTS_NOT_AVAILABLE.

    No forecast mocks: the job fails at `charts_fetching` before it
    ever reaches `forecast_prefetching`.
    """
    # Far off the seeded Chesapeake cell.
    payload = {
        "origin": {"lat": 42.0, "lon": -70.0, "name": "Boston"},
        "destination": {"lat": 42.2, "lon": -70.3, "name": "Gloucester"},
        "window": {
            "start_at": "2026-04-18T00:00:00Z",
            "end_at": "2026-04-18T03:00:00Z",
            "tz": "UTC",
        },
        "boat_profile_name": "default",
        "max_candidates": 2,
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
    assert body["status"] == "failed"
    assert body["error"]["code"] == "CHARTS_NOT_AVAILABLE"
    assert body["error"]["stage"] == "charts_fetching"
