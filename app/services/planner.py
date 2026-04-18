"""Voyage planner — drives a submission through the job stages.

`JobRegistry` owns the asyncio task lifecycle; `run_job` owns the
stage progression. The planner now enumerates a hourly departure grid
across the request window, routes every candidate in parallel under a
bounded executor, scores survivors, and emits the top-N as a
multi-`<rte>` GPX blob.

Chart data is still stubbed by `NullChartStore` — real ENC / OSM /
GEBCO ingest lands next (plan/15-charts-bathymetry).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from app.db import session_scope
from app.logging import get_logger
from app.models.voyage import Voyage
from app.observability import meter, tracer
from app.schemas.request import VoyageRequest
from app.services import boat_profiles
from app.services.charts import NullChartStore
from app.services.contingency import (
    BackupDestination,
    TapOut,
    decision_points,
    find_backup_destinations,
    find_tapouts,
)
from app.services.forecast_field import ForecastField
from app.services.jobs import set_stage, write_progress
from app.services.polars import Polar
from app.services.router import (
    BoatLimits,
    IsochronePoint,
    RouterError,
    RouteResult,
    plan_candidate,
)
from app.services.scorer import Score, score_candidate

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


@dataclass
class PlanState:
    voyage_id: str
    req: VoyageRequest
    forecast: ForecastField | None = None
    candidates: list[Candidate] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)


def _bbox_from_request(req: VoyageRequest, pad_deg: float = 0.5) -> tuple[float, float, float, float]:
    lat_min = min(req.origin.lat, req.destination.lat) - pad_deg
    lat_max = max(req.origin.lat, req.destination.lat) + pad_deg
    lon_min = min(req.origin.lon, req.destination.lon) - pad_deg
    lon_max = max(req.origin.lon, req.destination.lon) + pad_deg
    return (lat_min, lon_min, lat_max, lon_max)


def enumerate_departures(req: VoyageRequest, step_hours: int = 1) -> list[datetime]:
    """Hourly departure grid across `[window.start_at, window.end_at]`.

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
    await set_stage(state.voyage_id, "charts_fetching", pct=0.0, detail="null-chart-store")
    with _tracer.start_as_current_span("job.charts_fetching"):
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


async def _route_one(
    depart_at: datetime,
    state: PlanState,
    polar: Polar,
    charts: NullChartStore,
    boat: BoatLimits,
    sem: asyncio.Semaphore,
) -> tuple[datetime, RouteResult | None, str | None]:
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
                max_steps=168,
                arrival_tolerance_nm=0.5,
            )
            return depart_at, result, None
        except RouterError as exc:
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

    charts = NullChartStore()
    departures = enumerate_departures(state.req)
    _candidates_total.add(len(departures))

    sem = asyncio.Semaphore(4)  # BV_MAX_CONCURRENT_ROUTES

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
    await set_stage(state.voyage_id, "finalizing", pct=0.0, detail="contingencies + gpx")
    with _tracer.start_as_current_span("job.finalizing"):
        _derive_contingencies(state)
        gpx = _emit_gpx(state)
        coverage = json.dumps(
            {
                "forecast": "open-meteo-marine",
                "tides": None,
                "charts": {"source": "null-chart-store"},
                "skipped": state.skipped,
                "candidates_surfaced": len(state.candidates),
            }
        )
        async with session_scope() as session:
            row = await session.get(Voyage, state.voyage_id)
            if row is None:
                raise LookupError(f"voyage {state.voyage_id} not found")
            row.gpx_blob = gpx
            row.coverage_json = coverage


def _derive_contingencies(state: PlanState) -> None:
    dest_lat = state.req.destination.lat
    dest_lon = state.req.destination.lon
    for c in state.candidates:
        c.backup_destinations = find_backup_destinations(dest_lat, dest_lon)
        picks = decision_points(c.route.points)
        by_index: dict[int, list[TapOut]] = {}
        for p in picks:
            idx = c.route.points.index(p)
            by_index[idx] = find_tapouts(p)
        c.tapouts_by_index = by_index


# --- GPX emission --------------------------------------------------------


def _rtept(p: IsochronePoint, name: str = "", tapouts: list[TapOut] | None = None) -> str:
    ext_bits: list[str] = []
    if p.heading_deg is not None and p.bsp_kts > 0:
        ext_bits.append(
            f'<bv:leg bearingDeg="{p.heading_deg:.1f}" bspKts="{p.bsp_kts:.2f}"/>'
        )
    if tapouts:
        ext_bits.append("<bv:tapOut>")
        for t in tapouts:
            ext_bits.append(
                f'<bv:option name="{escape(t.name)}" '
                f'lat="{t.lat:.6f}" lon="{t.lon:.6f}" '
                f'detourNm="{t.detour_nm:.2f}" '
                f'type="{escape(t.type or "")}"/>'
            )
        ext_bits.append("</bv:tapOut>")
    extras = f"<extensions>{''.join(ext_bits)}</extensions>" if ext_bits else ""
    name_el = f"<name>{escape(name)}</name>" if name else ""
    return (
        f'<rtept lat="{p.lat:.6f}" lon="{p.lon:.6f}">'
        f"{name_el}<time>{p.t.isoformat()}</time>{extras}"
        f"</rtept>"
    )


def _emit_gpx(state: PlanState) -> bytes:
    """Emit a GPX 1.1 doc with one <rte> per surfaced candidate.

    `services/gpx.py` replaces this with a fully round-trip-safe
    serializer at M5 (plan/09); for now it's enough to produce a file
    that loads cleanly in OpenCPN and carries the `bv:` score + rank.
    """
    req = state.req
    origin_label = escape(req.origin.name or "Origin")
    dest_label = escape(req.destination.name or "Destination")

    rtes: list[str] = []
    for c in state.candidates:
        pts = c.route.points
        body: list[str] = []
        for idx, p in enumerate(pts):
            tapouts = c.tapouts_by_index.get(idx)
            if idx == 0:
                body.append(_rtept(p, "Origin"))
            elif idx == len(pts) - 1:
                body.append(_rtept(p, "Destination"))
            else:
                body.append(_rtept(p, tapouts=tapouts))
        body_str = "\n      ".join(body)

        backup_xml = ""
        if c.backup_destinations:
            options = "".join(
                f'<bv:option name="{escape(b.name)}" '
                f'lat="{b.lat:.6f}" lon="{b.lon:.6f}" '
                f'detourNm="{b.detour_nm:.2f}"/>'
                for b in c.backup_destinations
            )
            backup_xml = f"<bv:backupDestinations>{options}</bv:backupDestinations>"

        rtes.append(
            "  <rte>\n"
            f"    <name>Candidate {c.rank}: {origin_label} -> {dest_label}</name>\n"
            f"    <type>primary</type>\n"
            "    <extensions>"
            f'<bv:candidate rank="{c.rank}" '
            f'departAt="{c.depart_at.isoformat()}" '
            f'arriveAt="{c.route.reached_at.isoformat()}"/>'
            f'<bv:score total="{c.score.total:.2f}"/>'
            f"{backup_xml}"
            "</extensions>\n"
            f"    {body_str}\n"
            "  </rte>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="better-voyage/0.1"\n'
        '     xmlns="http://www.topografix.com/GPX/1/1"\n'
        '     xmlns:bv="https://better-voyage.app/gpx/1">\n'
        f"{chr(10).join(rtes)}\n"
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
    "Candidate",
    "PlanState",
    "PlannerError",
    "ProgressThrottle",
    "enumerate_departures",
    "run_job",
    "write_progress",
]
