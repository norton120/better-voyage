"""Voyage planner — drives a submission through the job stages.

`JobRegistry` owns the asyncio task lifecycle; `run_job` owns the
stage progression. Each stage consumes + produces a `PlanState`
passed through by reference, so intermediates (prefetched forecast
field, routed candidate, score) don't round-trip through the DB.

Chart data is still stubbed by `NullChartStore` — the real ENC / OSM /
GEBCO ingest is the next M2 slice (plan/15-charts-bathymetry).

`PlannerError` carries the job-level error code (plan/10 §errors) so
`JobRegistry._wrap` writes the right row state on failure.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from app.db import session_scope
from app.logging import get_logger
from app.models.voyage import Voyage
from app.observability import tracer
from app.schemas.request import VoyageRequest
from app.services.charts import NullChartStore
from app.services.forecast_field import ForecastField
from app.services.jobs import set_stage, write_progress
from app.services.polars import DEFAULT_POLAR_PATH, Polar
from app.services.router import BoatLimits, RouterError, plan_candidate
from app.services.scorer import Score, score_candidate

log = get_logger(__name__)
_tracer = tracer("app.services.planner")


class PlannerError(Exception):
    """Typed job-level failure; maps to voyages.error_code on terminal state."""

    def __init__(self, code: str, stage: str, detail: str | None = None) -> None:
        super().__init__(f"{code} at {stage}: {detail}")
        self.code = code
        self.stage = stage
        self.detail = detail


@dataclass
class ProgressThrottle:
    """Rate-limits progress writes on long runs (plan/16 §Progress shape)."""

    min_interval_s: float
    min_pct_delta: float
    _last_write: float = 0.0
    _last_pct: float = -1.0

    def should_write(self, pct: float) -> bool:
        now = time.monotonic()
        if self._last_pct < 0:
            self._last_write = now
            self._last_pct = pct
            return True
        if (now - self._last_write) >= self.min_interval_s or (
            abs(pct - self._last_pct) >= self.min_pct_delta
        ):
            self._last_write = now
            self._last_pct = pct
            return True
        return False


@dataclass
class PlanState:
    voyage_id: str
    req: VoyageRequest
    forecast: ForecastField | None = None
    route_points: list = field(default_factory=list)
    reached_at: object | None = None
    score: Score | None = None


def _bbox_from_request(req: VoyageRequest, pad_deg: float = 0.5) -> tuple[float, float, float, float]:
    lat_min = min(req.origin.lat, req.destination.lat) - pad_deg
    lat_max = max(req.origin.lat, req.destination.lat) + pad_deg
    lon_min = min(req.origin.lon, req.destination.lon) - pad_deg
    lon_max = max(req.origin.lon, req.destination.lon) + pad_deg
    return (lat_min, lon_min, lat_max, lon_max)


# --- stages --------------------------------------------------------------


async def _stage_charts_fetching(state: PlanState) -> None:
    await set_stage(state.voyage_id, "charts_fetching", pct=0.0, detail="null-chart-store")
    with _tracer.start_as_current_span("job.charts_fetching"):
        # Real ENC / OSM / GEBCO ingest lands with plan/15-charts-bathymetry.
        await asyncio.sleep(0)


async def _stage_charts_preprocessing(state: PlanState) -> None:
    await set_stage(state.voyage_id, "charts_preprocessing", pct=0.0, detail="null-chart-store")
    with _tracer.start_as_current_span("job.charts_preprocessing"):
        await asyncio.sleep(0)


async def _stage_forecast_prefetching(state: PlanState) -> None:
    await set_stage(state.voyage_id, "forecast_prefetching", pct=0.0, detail="fetching grid")
    bbox = _bbox_from_request(state.req)
    field_ = ForecastField(grid_res_deg=0.5)
    with _tracer.start_as_current_span("job.forecast_prefetching"):
        try:
            await field_.prefetch(bbox, state.req.window.start_at, state.req.window.end_at)
        except Exception as exc:
            raise PlannerError(
                "FORECAST_UNAVAILABLE", "forecast_prefetching", str(exc)[:400]
            ) from exc
    state.forecast = field_
    await write_progress(state.voyage_id, "forecast_prefetching", 1.0)


async def _stage_routing(state: PlanState) -> None:
    await set_stage(state.voyage_id, "routing", pct=0.0, detail="isochrone search")
    if state.forecast is None:
        raise PlannerError("INTERNAL_ERROR", "routing", "forecast not prefetched")
    polar = Polar.load(DEFAULT_POLAR_PATH)
    charts = NullChartStore()
    origin = (state.req.origin.lat, state.req.origin.lon)
    destination = (state.req.destination.lat, state.req.destination.lon)
    depart_at = state.req.window.start_at

    with _tracer.start_as_current_span(
        "job.routing",
        attributes={"voyage.id": state.voyage_id, "objective": state.req.objective},
    ):
        try:
            result = await asyncio.to_thread(
                plan_candidate,
                origin=origin,
                destination=destination,
                depart_at=depart_at,
                polar=polar,
                forecast=state.forecast,
                charts=charts,
                boat=BoatLimits(),
                step_minutes=60,
                max_steps=168,
                arrival_tolerance_nm=0.5,
            )
        except RouterError as exc:
            raise PlannerError(exc.code, "routing", exc.detail) from exc

    state.route_points = result.points
    state.reached_at = result.reached_at
    await write_progress(
        state.voyage_id,
        "routing",
        1.0,
        detail=f"{result.steps_used} steps; {len(result.points)} waypoints",
    )


async def _stage_scoring(state: PlanState) -> None:
    await set_stage(state.voyage_id, "scoring", pct=0.0)
    with _tracer.start_as_current_span("job.scoring"):
        state.score = score_candidate(state.route_points)
    await write_progress(
        state.voyage_id, "scoring", 1.0, detail=f"total={state.score.total}"
    )


async def _stage_finalizing(state: PlanState) -> None:
    await set_stage(state.voyage_id, "finalizing", pct=0.0, detail="emitting gpx")
    with _tracer.start_as_current_span("job.finalizing"):
        gpx = _emit_gpx(state)
        coverage = json.dumps(
            {
                "forecast": "open-meteo-marine",
                "tides": None,
                "charts": {"source": "null-chart-store"},
            }
        )
        async with session_scope() as session:
            row = await session.get(Voyage, state.voyage_id)
            if row is None:
                raise LookupError(f"voyage {state.voyage_id} not found")
            row.gpx_blob = gpx
            row.coverage_json = coverage


# --- GPX emission --------------------------------------------------------


def _emit_gpx(state: PlanState) -> bytes:
    """Emit a minimal GPX containing the routed candidate as a single rte.

    `services/gpx.py` replaces this with a full round-trip-safe
    serializer at M5; for now it's enough to produce a file that
    loads cleanly in OpenCPN and carries the `bv:` score extension.
    """
    req = state.req
    origin = escape(req.origin.name or "Origin")
    dest = escape(req.destination.name or "Destination")
    score_total = state.score.total if state.score else 0.0

    rtepts: list[str] = []
    for p in state.route_points:
        name = (
            "Origin" if p.parent is None
            else "Destination" if p.lat == req.destination.lat and p.lon == req.destination.lon
            else ""
        )
        time_str = p.t.isoformat()
        extras = ""
        if p.heading_deg is not None and p.bsp_kts > 0:
            extras = (
                f'<extensions><bv:leg bearingDeg="{p.heading_deg:.1f}" '
                f'bspKts="{p.bsp_kts:.2f}"/></extensions>'
            )
        rtepts.append(
            f'<rtept lat="{p.lat:.6f}" lon="{p.lon:.6f}">'
            f'{("<name>" + escape(name) + "</name>") if name else ""}'
            f'<time>{time_str}</time>{extras}'
            f"</rtept>"
        )

    route_name = f"{origin} -> {dest}"
    rte = (
        "  <rte>\n"
        f"    <name>{route_name}</name>\n"
        f"    <extensions><bv:score total=\"{score_total:.2f}\"/></extensions>\n"
        f"    {chr(10).join(rtepts)}\n"
        "  </rte>\n"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="better-voyage/0.1"\n'
        '     xmlns="http://www.topografix.com/GPX/1/1"\n'
        '     xmlns:bv="https://better-voyage.app/gpx/1">\n'
        f"{rte}"
        "</gpx>\n"
    ).encode()


# --- top-level entry -----------------------------------------------------


async def run_job(voyage_id: str) -> None:
    async with session_scope() as session:
        row = await session.get(Voyage, voyage_id)
        if row is None:
            raise LookupError(f"voyage {voyage_id} not found")
        req = VoyageRequest.model_validate_json(row.request_json)

    state = PlanState(voyage_id=voyage_id, req=req)
    await _stage_charts_fetching(state)
    await _stage_charts_preprocessing(state)
    await _stage_forecast_prefetching(state)
    await _stage_routing(state)
    await _stage_scoring(state)
    await _stage_finalizing(state)


__all__ = [
    "PlanState",
    "PlannerError",
    "ProgressThrottle",
    "run_job",
    "write_progress",
]
