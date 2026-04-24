"""Voyage planner — drives a submission through the job stages.

`JobRegistry` owns the asyncio task lifecycle; `run_job` owns the
stage progression. The planner now enumerates a hourly departure grid
across the request window, routes every candidate in parallel under a
bounded executor, scores survivors, and emits the top-N as a
multi-`<rte>` GPX blob.

Chart data goes through `ChartStore` (plan/15-charts-bathymetry):
`_stage_charts_fetching` calls `ensure_coverage` to fetch + preprocess
NOAA ENC cells and an OSM extract, loads the GEBCO slice, and builds
per-layer STRtrees. `ChartsCoverageError` / `ChartsFetchError` map to
`CHARTS_NOT_AVAILABLE` / `CHARTS_FETCH_FAILED` terminal failures.
Tests use `NullChartStore` via `BV_CHART_STORE_MODE=null`.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from app.db import session_scope
from app.logging import get_logger
from app.models.voyage import Voyage
from app.observability import meter, tracer
from app.services.geo import distance_nm
from app.schemas.request import VoyageRequest
from app.services import boat_profiles
from app.services.charts import (
    ChartCoverage,
    ChartsCoverageError,
    ChartsFetchError,
    NullChartStore,
    get_chart_store,
)
from app.services.charts import ChartStore as _ChartStore
from app.services.contingency import (
    BackupDestination,
    EscapeHatch,
    TapOut,
    decision_points,
    find_backup_destinations,
    find_tapouts,
    plan_escape_hatches,
)
from app.services.forecast_field import ForecastField
from app.services.geo import advance
from app.services.gpx import emit_voyage
from app.services.jobs import set_stage, write_progress
from app.services.polars import Polar
from app.services.router import (
    BoatLimits,
    RouterError,
    RouteResult,
    plan_candidate,
)
from app.services.scorer import Score, score_candidate
from app.services.summary import Summary, summarize

log = get_logger(__name__)
_tracer = tracer("app.services.planner")
_m = meter("app.services.planner")
_candidates_total = _m.create_counter("bv.voyages.candidates_total", unit="1")
_candidates_rejected = _m.create_counter("bv.voyages.candidates_rejected", unit="1")


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
class Candidate:
    rank: int  # 1-based
    depart_at: datetime
    route: RouteResult
    score: Score
    backup_destinations: list[BackupDestination] = field(default_factory=list)
    # Map from rtept index within route.points to its tap-out list.
    tapouts_by_index: dict[int, list[TapOut]] = field(default_factory=dict)
    escape_hatches: list[EscapeHatch] = field(default_factory=list)
    summary: Summary | None = None


@dataclass
class PlanState:
    voyage_id: str
    req: VoyageRequest
    forecast: ForecastField | None = None
    candidates: list[Candidate] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    # Oldest `fetched_at` among any upstream cache row that served
    # stale during prefetch. Lifted off `forecast.stale_at` so it
    # survives after the `ForecastField` is discarded.
    forecast_stale_at: datetime | None = None
    # Stashed during routing for reuse in contingency generation.
    polar: Polar | None = None
    boat: BoatLimits | None = None
    charts: _ChartStore | NullChartStore | None = None
    charts_coverage: ChartCoverage | None = None


def _bbox_from_request(
    req: VoyageRequest, min_pad_deg: float = 0.5
) -> tuple[float, float, float, float]:
    """Forecast bbox around the passage, padded for isochrone expansion.

    A fixed 0.5° pad is too narrow once the passage extends beyond ~30 nm:
    isochrone fans easily drift that much laterally chasing wind, and
    any frontier point outside the forecast bbox returns `env_none`,
    collapsing the frontier. Scale the pad with the passage extent so
    long passages get proportionally more room to breathe.
    """
    lat_extent = abs(req.origin.lat - req.destination.lat)
    lon_extent = abs(req.origin.lon - req.destination.lon)
    pad_deg = max(min_pad_deg, 0.5 * max(lat_extent, lon_extent))
    lat_min = min(req.origin.lat, req.destination.lat) - pad_deg
    lat_max = max(req.origin.lat, req.destination.lat) + pad_deg
    lon_min = min(req.origin.lon, req.destination.lon) - pad_deg
    lon_max = max(req.origin.lon, req.destination.lon) + pad_deg
    return (lat_min, lon_min, lat_max, lon_max)


def _adaptive_step_hours(req: VoyageRequest) -> int:
    """Step size adapts to window length: hourly for short windows (≤6 h),
    3-hourly for long windows (>12 h). Fixed-1-hour enumeration on a
    72 h window generates 73 candidates and the router serialises them
    against a single ChartStore — at ~1-3 s per candidate on real
    charts the pipeline wallclock balloons with no meaningful gain
    since `max_candidates` already caps surfaced results to the top N.
    """
    window_h = (req.window.end_at - req.window.start_at).total_seconds() / 3600.0
    if window_h <= 6:
        return 1
    if window_h <= 12:
        return 2
    return 3


def enumerate_departures(req: VoyageRequest, step_hours: int = 1) -> list[datetime]:
    """Hourly (default) departure grid across `[window.start_at, window.end_at]`.

    Local-time constraints (`earliest_departure_local_time`,
    `latest_departure_local_time`) in the window's IANA tz drop
    candidates outside the preferred window per plan/07 §Enumeration.
    Night-arrival filtering happens post-routing since arrival time
    isn't known until the router finishes.
    """
    try:
        tz = ZoneInfo(req.window.tz)
    except Exception:
        tz = ZoneInfo("UTC")

    earliest = req.window.earliest_departure_local_time
    latest = req.window.latest_departure_local_time

    out: list[datetime] = []
    t = req.window.start_at
    while t <= req.window.end_at:
        if _within_local_time_window(t, tz, earliest, latest):
            out.append(t)
        t += timedelta(hours=step_hours)
    return out


def _within_local_time_window(
    t: datetime, tz: ZoneInfo, earliest: dtime | None, latest: dtime | None
) -> bool:
    if earliest is None and latest is None:
        return True
    local = t.astimezone(tz).time()
    if earliest is not None and latest is not None:
        if earliest <= latest:
            return earliest <= local <= latest
        # Wraps midnight (e.g. 22:00-06:00).
        return local >= earliest or local <= latest
    if earliest is not None:
        return local >= earliest
    if latest is not None:
        return local <= latest
    return True


def _is_night_local(t: datetime, tz: ZoneInfo) -> bool:
    """22:00-06:00 local per plan/07 §Enumeration."""
    h = t.astimezone(tz).hour
    return h >= 22 or h < 6


# --- stages --------------------------------------------------------------


async def _stage_charts_fetching(state: PlanState) -> None:
    """Fetch + preprocess chart data for the voyage bbox (plan/15).

    The `charts_fetching` and `charts_preprocessing` stages are driven
    by a single `ChartStore.ensure_coverage` call — the store does its
    own fetch → preprocess pipeline internally. We split the two
    stages for trace-topology parity with plan/16: `charts_fetching`
    covers ensure_coverage end-to-end, `charts_preprocessing` is a
    noop placeholder that set_stage's for the stage_transitions metric.
    """
    await set_stage(state.voyage_id, "charts_fetching", pct=0.0, detail="ensure_coverage")
    store = get_chart_store()
    state.charts = store
    bbox = _bbox_from_request(state.req)
    with _tracer.start_as_current_span(
        "job.charts_fetching",
        attributes={
            "voyage.id": state.voyage_id,
            "charts.store": store.__class__.__name__,
        },
    ):
        try:
            await store.ensure_coverage(bbox)
        except ChartsCoverageError as exc:
            raise PlannerError(
                "CHARTS_NOT_AVAILABLE", "charts_fetching", str(exc)[:400]
            ) from exc
        except ChartsFetchError as exc:
            raise PlannerError(
                "CHARTS_FETCH_FAILED", "charts_fetching", str(exc)[:400]
            ) from exc
    state.charts_coverage = await store.coverage(bbox)
    await _write_coverage(state)
    profile = await boat_profiles.get(state.req.boat_profile_name)
    if profile is None:
        raise PlannerError(
            "BOAT_PROFILE_NOT_FOUND",
            "charts_fetching",
            state.req.boat_profile_name,
        )
    _snap_endpoints_to_water(
        state.req, store, profile.draft_m + profile.min_depth_m
    )
    await write_progress(state.voyage_id, "charts_fetching", 1.0)


# Spiral-snap parameters. Radii expand geometrically so we prefer
# tiny nudges (a click on the wrong side of a pixel on a coarse bay
# chart) over large relocations. 2 nm is the hard cap — beyond that
# the user almost certainly picked the wrong spot entirely, and
# silently relocating them > 2 nm would be more confusing than a
# clean error.
_SNAP_RADII_NM: tuple[float, ...] = (0.05, 0.10, 0.20, 0.40, 0.80, 1.50, 2.00)
_SNAP_BEARINGS: tuple[float, ...] = tuple(float(i * 15) for i in range(24))


def _snap_endpoints_to_water(
    req: VoyageRequest,
    store: _ChartStore | NullChartStore,
    required_depth_m: float,
) -> None:
    """Silently move endpoints to the nearest navigable water.

    Two failure modes motivate this: (1) user clicks on a land polygon
    pixel, (2) user clicks in water that's shallower than the boat's
    `draft_m + min_depth_m`. Case 2 is the nasty one — without the
    snap, every candidate departure grinds through all `max_steps`
    isochrones before `ROUTE_TIMEOUT` because no pruned point can
    ever satisfy the arrival-tolerance check (the destination's
    approach cell fails the depth filter). With the snap, both cases
    converge on "start/end a few hundred meters from the click, plan
    normally" — which matches what a skipper would do at the helm.

    Raises `ENDPOINT_ON_LAND` only if no navigable water exists
    within 2 nm of the original point.
    """
    for label, coord in (("origin", req.origin), ("destination", req.destination)):
        if _point_is_navigable(store, coord.lat, coord.lon, required_depth_m):
            continue
        snapped = _find_nearest_navigable(
            store, coord.lat, coord.lon, required_depth_m
        )
        if snapped is None:
            raise PlannerError(
                "ENDPOINT_ON_LAND",
                "charts_fetching",
                f"{label} ({coord.lat:.5f}, {coord.lon:.5f}) has no navigable "
                f"water (need ≥{required_depth_m:.1f} m) within 2 nm",
            )
        log.info(
            "endpoint snapped to navigable water",
            extra={
                "label": label,
                "from": (coord.lat, coord.lon),
                "to": snapped,
                "required_depth_m": required_depth_m,
            },
        )
        coord.lat, coord.lon = snapped


def _point_is_navigable(
    store: _ChartStore | NullChartStore, lat: float, lon: float, required_depth_m: float
) -> bool:
    if store.distance_to_land_nm(lat, lon) <= 0.0:
        return False
    depth = store.chart_depth(lat, lon)
    return depth is not None and depth >= required_depth_m


def _find_nearest_navigable(
    store: _ChartStore | NullChartStore,
    lat: float,
    lon: float,
    required_depth_m: float,
) -> tuple[float, float] | None:
    for r_nm in _SNAP_RADII_NM:
        for bearing in _SNAP_BEARINGS:
            cand_lat, cand_lon = advance(lat, lon, bearing, r_nm)
            if _point_is_navigable(store, cand_lat, cand_lon, required_depth_m):
                return cand_lat, cand_lon
    return None


async def _stage_charts_preprocessing(state: PlanState) -> None:
    """Stage boundary for traces + metrics; real preprocessing happens
    inside `ensure_coverage` in the previous stage."""
    await set_stage(state.voyage_id, "charts_preprocessing", pct=1.0, detail="done")
    with _tracer.start_as_current_span("job.charts_preprocessing"):
        await asyncio.sleep(0)


async def _stage_forecast_prefetching(state: PlanState) -> None:
    await set_stage(state.voyage_id, "forecast_prefetching", pct=0.0, detail="fetching grid")
    bbox = _bbox_from_request(state.req)
    field_ = ForecastField(grid_res_deg=0.5)
    # Forecast must cover departure-window + a passage-length buffer.
    # A candidate departing at window.end_at needs forecast data for
    # the full passage duration beyond that — otherwise forecast.at()
    # returns None mid-route and the frontier collapses with env_none.
    # Cap at the 7-day Open-Meteo marine horizon.
    req = state.req
    passage_nm = distance_nm(
        req.origin.lat, req.origin.lon, req.destination.lat, req.destination.lon
    )
    # Conservative 3 kt effective speed — covers light-air legs and a
    # long tacking track. Clamp between 24 h and 168 h.
    passage_buffer_h = min(168.0, max(24.0, passage_nm / 3.0))
    prefetch_end = req.window.end_at + timedelta(hours=passage_buffer_h)
    with _tracer.start_as_current_span("job.forecast_prefetching"):
        try:
            await field_.prefetch(bbox, req.window.start_at, prefetch_end)
        except Exception as exc:
            raise PlannerError(
                "FORECAST_UNAVAILABLE", "forecast_prefetching", str(exc)[:400]
            ) from exc
    state.forecast = field_
    state.forecast_stale_at = field_.stale_at
    if field_.stale_at is not None:
        # Persist an early coverage snapshot so a later offline failure
        # can still surface the staleness hint on GET /voyages/{id}.
        await _write_coverage(state)
    await write_progress(state.voyage_id, "forecast_prefetching", 1.0)


async def _route_one(
    depart_at: datetime,
    state: PlanState,
    polar: Polar,
    charts: _ChartStore | NullChartStore,
    boat: BoatLimits,
    sem: asyncio.Semaphore,
) -> tuple[datetime, RouteResult | None, str | None]:
    assert state.forecast is not None, "forecast prefetched before routing"
    async with sem:
        try:
            result = await asyncio.to_thread(
                plan_candidate,
                origin=(state.req.origin.lat, state.req.origin.lon),
                destination=(state.req.destination.lat, state.req.destination.lon),
                depart_at=depart_at,
                polar=polar,
                forecast=state.forecast,
                charts=charts,
                boat=boat,
                objective=state.req.objective,
                step_minutes=60,
                max_passage_hours=168,
                arrival_tolerance_nm=0.5,
            )
            return depart_at, result, None
        except RouterError as exc:
            log.info(
                "planner.candidate_failed",
                voyage_id=state.voyage_id,
                depart_at=depart_at.isoformat(),
                code=exc.code,
                detail=exc.detail,
            )
            return depart_at, None, exc.code


async def _stage_routing(state: PlanState) -> None:
    await set_stage(state.voyage_id, "routing", pct=0.0, detail="enumerating departures")
    if state.forecast is None:
        raise PlannerError("INTERNAL_ERROR", "routing", "forecast not prefetched")

    profile = await boat_profiles.get(state.req.boat_profile_name)
    if profile is None:
        raise PlannerError(
            "BOAT_PROFILE_NOT_FOUND", "routing", state.req.boat_profile_name
        )
    try:
        polar = Polar.load(profile.polar_path)
    except Exception as exc:
        raise PlannerError(
            "INVALID_BOAT", "routing", f"polar load failed: {exc}"
        ) from exc
    boat = BoatLimits(
        draft_m=profile.draft_m,
        min_depth_m=profile.min_depth_m,
        max_wind_kts=profile.max_wind_kts,
        max_seas_m=profile.max_seas_m,
    )

    charts = state.charts
    if charts is None:
        # Defensive: charts_fetching should have set this. Fall back to
        # the null stub so we don't crash on the singleton miss.
        charts = NullChartStore()
        state.charts = charts
    state.polar = polar
    state.boat = boat
    departures = enumerate_departures(state.req, step_hours=_adaptive_step_hours(state.req))
    _candidates_total.add(len(departures))

    # Serial routing — shapely PreparedGeometry (re-enabled below) is
    # not guaranteed thread-safe under shapely 2.1; observed silent
    # segfaults under asyncio.to_thread concurrency on real charts.
    # Revisit when a safer pattern is confirmed (per-thread prep caches
    # or a shapely 2.2+ guarantee).
    sem = asyncio.Semaphore(1)

    with _tracer.start_as_current_span(
        "job.routing",
        attributes={
            "voyage.id": state.voyage_id,
            "objective": state.req.objective,
            "departures": len(departures),
        },
    ):
        async def _tick(total: int, done: list[int]) -> None:
            done[0] += 1
            await write_progress(
                state.voyage_id,
                "routing",
                done[0] / total,
                detail=f"{done[0]} / {total} candidates routed",
            )

        done = [0]
        async def _one(t: datetime) -> tuple[datetime, RouteResult | None, str | None]:
            r = await _route_one(t, state, polar, charts, boat, sem)
            await _tick(len(departures), done)
            return r

        results = await asyncio.gather(*[_one(t) for t in departures])

    try:
        tz = ZoneInfo(state.req.window.tz)
    except Exception:
        tz = ZoneInfo("UTC")

    survivors: list[tuple[datetime, RouteResult, Score]] = []
    skipped: dict[str, int] = {}
    for depart_at, route, err in results:
        if err is not None:
            skipped[err.lower()] = skipped.get(err.lower(), 0) + 1
            _candidates_rejected.add(1, {"reason": err})
            continue
        assert route is not None
        if not profile.night_sailing_ok and _is_night_local(route.reached_at, tz):
            skipped["night_arrival"] = skipped.get("night_arrival", 0) + 1
            _candidates_rejected.add(1, {"reason": "NIGHT_ARRIVAL"})
            continue
        s = score_candidate(route.points)
        survivors.append((depart_at, route, s))

    # Rank: -score desc, depart_at asc (stable).
    survivors.sort(key=lambda x: (-x[2].total, x[0]))
    top = survivors[: state.req.max_candidates]

    state.candidates = [
        Candidate(rank=i + 1, depart_at=d, route=r, score=s)
        for i, (d, r, s) in enumerate(top)
    ]
    state.skipped = skipped

    if not state.candidates:
        if state.forecast_stale_at is not None:
            raise PlannerError(
                "OFFLINE_NO_ROUTE",
                "routing",
                (
                    f"no candidates survived with stale forecast "
                    f"(oldest cache fetched_at={state.forecast_stale_at.isoformat()}); "
                    f"skipped={skipped}"
                ),
            )
        raise PlannerError(
            "ROUTE_BLOCKED",
            "routing",
            f"no candidates survived; skipped={skipped}",
        )


async def _stage_scoring(state: PlanState) -> None:
    """Scoring already happened inline during routing; this stage is kept
    for trace-topology parity with plan/16 and for a future split (where
    scoring a surfaced candidate runs contingencies + summary)."""
    await set_stage(state.voyage_id, "scoring", pct=1.0, detail=f"{len(state.candidates)} ranked")
    with _tracer.start_as_current_span("job.scoring"):
        await asyncio.sleep(0)


async def _stage_finalizing(state: PlanState) -> None:
    await set_stage(state.voyage_id, "finalizing", pct=0.0, detail="contingencies + summary + gpx")
    with _tracer.start_as_current_span("job.finalizing"):
        _derive_contingencies(state)
        await _render_summaries(state)
        gpx = emit_voyage(state)
        coverage = json.dumps(_coverage_payload(state))
        async with session_scope() as session:
            row = await session.get(Voyage, state.voyage_id)
            if row is None:
                raise LookupError(f"voyage {state.voyage_id} not found")
            row.gpx_blob = gpx
            row.coverage_json = coverage


def _coverage_payload(state: PlanState) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "forecast": "open-meteo-marine",
        "tides": None,
        "charts": _charts_coverage_block(state),
        "skipped": dict(state.skipped),
        "candidates_surfaced": len(state.candidates),
    }
    if state.forecast_stale_at is not None:
        payload["forecast_stale_at"] = state.forecast_stale_at.isoformat()
    if state.candidates:
        payload["contingencies"] = {
            "backup_destinations": sum(len(c.backup_destinations) for c in state.candidates),
            "tap_outs": sum(
                len(v) for c in state.candidates for v in c.tapouts_by_index.values()
            ),
            "escape_hatch_routes": sum(len(c.escape_hatches) for c in state.candidates),
        }
    return payload


def _charts_coverage_block(state: PlanState) -> dict[str, Any]:
    """Per plan/15 §Coverage block + plan/10 §voyage.bv:coverage.charts."""
    cov = state.charts_coverage
    if cov is None:
        # charts_fetching stage didn't run (e.g. NullChartStore path or
        # early failure); surface a marker the UI can skip rendering.
        if isinstance(state.charts, NullChartStore):
            return {"source": "null-chart-store"}
        return {"source": "unknown"}
    return {
        "enc_cells": cov.enc_cells,
        "osm_extracts": cov.osm_extracts,
        "gebco_tile": cov.gebco_tile,
        "fetched_at": cov.fetched_at.isoformat() if cov.fetched_at else None,
        "tide_modulated_depth": cov.tide_modulated_depth,
    }


async def _write_coverage(state: PlanState) -> None:
    """Persist the current coverage snapshot mid-run.

    Called after forecast prefetch flags staleness so a subsequent
    offline failure still has something to show the client.
    """
    coverage = json.dumps(_coverage_payload(state))
    async with session_scope() as session:
        row = await session.get(Voyage, state.voyage_id)
        if row is None:
            return
        row.coverage_json = coverage


def _derive_contingencies(state: PlanState) -> None:
    dest_lat = state.req.destination.lat
    dest_lon = state.req.destination.lon
    assert state.forecast is not None
    assert state.polar is not None
    assert state.boat is not None
    assert state.charts is not None
    for c in state.candidates:
        c.backup_destinations = find_backup_destinations(dest_lat, dest_lon)
        picks = decision_points(c.route.points)
        by_index: dict[int, list[TapOut]] = {}
        picked_indices: list[int] = []
        for p in picks:
            idx = c.route.points.index(p)
            picked_indices.append(idx)
            by_index[idx] = find_tapouts(p)
        c.tapouts_by_index = by_index
        c.escape_hatches = plan_escape_hatches(
            primary=c.route,
            decision_indices=picked_indices,
            boat=state.boat,
            forecast=state.forecast,
            polar=state.polar,
            charts=state.charts,
            objective=state.req.objective,
        )


async def _render_summaries(state: PlanState) -> None:
    """Generate one 1-3 sentence recap per surfaced candidate."""
    for c in state.candidates:
        c.summary = await summarize(
            candidate=c, req=state.req, tz_name=state.req.window.tz
        )


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
    "Candidate",
    "PlanState",
    "PlannerError",
    "ProgressThrottle",
    "enumerate_departures",
    "run_job",
    "write_progress",
]
