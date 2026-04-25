"""Worker entry point for parallel `plan_candidate` execution.

Used by `planner._stage_routing` to dispatch candidate routings across
a `ProcessPoolExecutor`. Each worker process is a fresh Python
interpreter with its own GEOS / shapely state, which sidesteps the
shapely 2.1 PreparedGeometry thread-safety hazard that forced
serial-routing in the same-process / asyncio.to_thread model.

Per-process state is cached in a module-level `_charts` so the heavy
ChartStore reload (parsing preprocessed GeoJSON, building STRtrees,
prepping land/obstacle geoms) happens once per worker per voyage
rather than once per candidate. The ChartStore reads from the same
on-disk preprocessed cache the main process wrote.

Lives in its own module — separate from `planner.py` — so workers
can import it without dragging the entire planner orchestration
graph (DB session pools, etc.) into every subprocess.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.charts import ChartStore
    from app.services.forecast_field import ForecastField
    from app.services.router import BoatLimits, RouteResult


# Per-worker ChartStore. Lazily populated on the first call into the
# worker for a given (charts_dir, gebco_path, bbox) tuple. Subsequent
# candidates in the same voyage reuse the same loaded store.
_charts: "ChartStore | None" = None
_charts_key: tuple[str, str | None, tuple[float, float, float, float]] | None = None


def _ensure_worker_charts(
    charts_dir: str,
    gebco_path: str | None,
    bbox: tuple[float, float, float, float],
) -> "ChartStore":
    """Load — or reuse — this worker's ChartStore for the given bbox.

    Uses `ChartStore.load_existing_for_bbox` which only reads the
    preprocessed GeoJSON files the parent already wrote during
    `_stage_charts_fetching`. No fetches, no preprocess writes —
    eliminates the worker-vs-worker race on the on-disk preprocess
    cache that broke an earlier multi-worker run with a JSON parse
    error mid-load.
    """
    global _charts, _charts_key
    key = (charts_dir, gebco_path, bbox)
    if _charts is not None and _charts_key == key:
        return _charts
    from app.services.charts import ChartStore

    store = ChartStore(
        base_dir=Path(charts_dir),
        gebco_path=Path(gebco_path) if gebco_path else None,
    )
    store.load_existing_for_bbox(bbox)
    _charts = store
    _charts_key = key
    return store


def route_in_worker(
    *,
    depart_at: datetime,
    origin: tuple[float, float],
    destination: tuple[float, float],
    forecast: "ForecastField",
    polar_path: str,
    boat: "BoatLimits",
    bbox: tuple[float, float, float, float],
    charts_dir: str,
    gebco_path: str | None,
    chart_store_mode: str,
    objective: str,
    safety_margin_land_nm: float,
    wallclock_budget_s: float,
    max_passage_hours: float,
    arrival_tolerance_nm: float,
) -> tuple[datetime, "RouteResult | None", str | None]:
    """Worker-side wrapper around `plan_candidate`. Returns the same
    triple as `_route_one` so the planner's downstream code is
    unchanged.
    """
    from app.services.polars import Polar
    from app.services.router import RouterError, plan_candidate

    if chart_store_mode == "real":
        charts: Any = _ensure_worker_charts(charts_dir, gebco_path, bbox)
    else:
        from app.services.charts import NullChartStore
        charts = NullChartStore()

    polar = Polar.load(Path(polar_path))
    try:
        result = plan_candidate(
            origin=origin,
            destination=destination,
            depart_at=depart_at,
            polar=polar,
            forecast=forecast,
            charts=charts,
            boat=boat,
            objective=objective,  # type: ignore[arg-type]
            safety_margin_land_nm=safety_margin_land_nm,
            wallclock_budget_s=wallclock_budget_s,
            max_passage_hours=max_passage_hours,
            arrival_tolerance_nm=arrival_tolerance_nm,
        )
        return depart_at, result, None
    except RouterError as exc:
        # Detail is preserved on the exception so the parent process
        # can log it. We can't pickle a custom exception subclass with
        # extra attrs reliably across processes, so we return the
        # serialized detail in the third tuple slot — same shape the
        # planner already handles for serial routing.
        code_with_detail = (
            f"{exc.code}|{exc.detail}" if exc.detail else exc.code
        )
        return depart_at, None, code_with_detail
