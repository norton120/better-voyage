"""Chart-data point lookups (plan/15 §ChartStore, issue 01).

One endpoint so far: `GET /charts/point?lat=..&lon=..` — returns a
compact water/depth/distance view for a single coordinate drawn from
the same `ChartStore` the router queries. Lets the UI decide whether
a user's map pick is navigable *before* a voyage submission runs
through `charts_fetching` → `forecast_prefetching` → routing.

Coverage is opportunistic: the endpoint does not block on downloads.
If the relevant bbox isn't loaded yet, it reports `coverage_loaded:
false` (and kicks off a background `ensure_coverage` so subsequent
clicks can answer). The authoritative gate is still server-side at
submit time — see `planner._stage_validate_endpoints`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.logging import get_logger
from app.services.charts import (
    ChartsCoverageError,
    ChartsFetchError,
    get_chart_store,
)
from app.services.charts import ChartStore as _ChartStore

log = get_logger(__name__)
router = APIRouter(prefix="/charts", tags=["charts"])

# Padding around the clicked point for the bbox we ask the store to
# cover. 0.5° matches `_bbox_from_request` — wide enough that the
# nearest land geometry is inside the loaded sources, narrow enough
# that we're not pulling a continent on every click.
_POINT_PAD_DEG = 0.5


@router.get("/point", summary="Water / depth / land distance at a point")
async def point(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> dict[str, Any]:
    store = get_chart_store()
    bbox = (
        lat - _POINT_PAD_DEG,
        lon - _POINT_PAD_DEG,
        lat + _POINT_PAD_DEG,
        lon + _POINT_PAD_DEG,
    )

    try:
        cov = await store.coverage(bbox)
    except Exception as exc:  # defensive — coverage() is read-only
        log.warning("charts.point.coverage_failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARTS_UNAVAILABLE", "detail": str(exc)[:200]},
        ) from exc

    covered = not cov.gaps
    if isinstance(store, _ChartStore):
        # Real store also requires bathymetry — distance/depth answers
        # are only meaningful once GEBCO is loaded for this bbox.
        covered = covered and cov.gebco_tile is not None

    if not covered:
        # Warm the cache so the next click gets a real answer. The
        # per-bbox lock inside `ensure_coverage` dedupes repeated calls.
        if isinstance(store, _ChartStore):
            asyncio.create_task(
                _warm_coverage(store, bbox), name="charts.point.warm"
            )
        return {
            "in_water": None,
            "depth_m": None,
            "distance_to_land_nm": None,
            "coverage_loaded": False,
        }

    dist_nm = store.distance_to_land_nm(lat, lon)
    depth = store.chart_depth(lat, lon)
    return {
        # `distance_to_land_nm` returns 0.0 when the point sits inside
        # a land polygon. Strictly positive means outside all land.
        "in_water": dist_nm > 0.0,
        "depth_m": depth,
        "distance_to_land_nm": dist_nm,
        "coverage_loaded": True,
    }


async def _warm_coverage(store: _ChartStore, bbox: tuple[float, float, float, float]) -> None:
    try:
        await store.ensure_coverage(bbox)
    except (ChartsCoverageError, ChartsFetchError) as exc:
        log.info("charts.point.warm_failed", bbox=bbox, error=str(exc))
    except Exception as exc:  # pragma: no cover — background logging only
        log.warning("charts.point.warm_crashed", bbox=bbox, error=str(exc))


__all__ = ["router"]
