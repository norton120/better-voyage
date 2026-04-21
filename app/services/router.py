"""Isochrone weather-routing kernel.

Synchronous, numpy-light. Dispatched from the planner via
`asyncio.to_thread` so it doesn't pin the event loop (plan/02 §Async
model).

Pipeline per step `dt` (plan/04 §Core loop):

1. For each surviving isochrone point, sample forecast at `t`.
2. For each heading in the fan, compute true-wind angle, polar BSP.
3. Reject under hard wind / sea limits.
4. Advance geodesically (boat speed along heading + current drift).
5. Let the ChartStore reject land / obstacle / restricted / shallow.
6. Accumulate an objective-dependent cost on the new point.
7. Prune the frontier into N sectors by axis-aligned progress, biased
   by the point's accumulated cost.

Terminates when any point reaches the destination within tolerance,
or the step budget is exhausted.

Objectives (plan/04 §Objective function) tune the pruning metric:

- `fastest`      — no cost; progress alone wins.
- `comfortable`  — wave_height squared and wind-over-20 accumulate.
- `short_tacks`  — heading changes above 60° accumulate as maneuvers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import cos, radians
from typing import TYPE_CHECKING, Literal

from app.observability import meter
from app.services.geo import advance, bearing_deg, distance_nm, relative_wind_angle

if TYPE_CHECKING:
    from app.services.charts import ChartStoreProtocol as ChartStore
    from app.services.forecast_field import Env, ForecastField
    from app.services.polars import Polar


_m = meter("app.services.router")
_steps = _m.create_histogram(
    "bv.router.steps",
    description="Isochrone steps executed per plan_candidate call",
    unit="1",
)
_propagations_per_step = _m.create_histogram(
    "bv.router.propagations_per_step",
    description="Number of new frontier points generated per isochrone step, pre-prune",
    unit="1",
)
_wallclock = _m.create_histogram(
    "bv.router.wallclock_seconds",
    description="Wallclock duration of one plan_candidate call",
    unit="s",
)
_outcomes = _m.create_counter(
    "bv.router.outcomes",
    description="Terminal outcome of a plan_candidate call",
    unit="1",
)


Objective = Literal["fastest", "comfortable", "short_tacks"]


@dataclass
class BoatLimits:
    draft_m: float = 1.8
    min_depth_m: float = 0.5
    max_wind_kts: float = 30.0
    max_seas_m: float = 2.5
    min_bsp_kts: float = 0.3


@dataclass
class IsochronePoint:
    lat: float
    lon: float
    t: datetime
    parent: IsochronePoint | None = None
    heading_deg: float | None = None
    bsp_kts: float = 0.0
    env: Env | None = None
    accumulated_cost: float = 0.0


@dataclass
class RouteResult:
    points: list[IsochronePoint]  # origin → destination, chronological
    reached_at: datetime
    steps_used: int
    objective: Objective = "fastest"
    # Debug only; not part of the plan contract.
    isochrones: list[list[IsochronePoint]] = field(default_factory=list)


class RouterError(Exception):
    """Router-level failure. `code` aligns with plan/10 §errors."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --- heading fan ------------------------------------------------------------


def heading_fan(
    course_to_dest: float,
    coarse_step: int = 10,
    fine_step: int = 3,
    fine_radius: int = 20,
) -> list[float]:
    fine = {(course_to_dest + d) % 360.0 for d in range(-fine_radius, fine_radius + 1, fine_step)}
    coarse = {float(h) for h in range(0, 360, coarse_step)}
    return sorted(fine | coarse)


def heading_fan_fine(course_to_dest: float) -> list[float]:
    """Fan for near-shore stepping.

    Coarse every 10° (same as default), fine every 2° within ±30° of the
    course (vs. 3° within ±20° default). Net ~55 headings — only
    modestly denser than the default 50. The graduated step size is
    already ~6x finer (10-min vs 60-min), so path granularity comes
    from short steps, not from more headings per step. Over-densifying
    the fan here turns each near-shore step into a ~10-second compute
    spike under real ChartStore queries.
    """
    return heading_fan(course_to_dest, coarse_step=10, fine_step=2, fine_radius=30)


# --- objective costs --------------------------------------------------------

# How strongly accumulated cost biases the per-sector pruning choice.
# Larger = objective cost matters more vs. raw progress. Keeping these low
# ensures the frontier always advances.
_OBJECTIVE_WEIGHT: dict[str, float] = {
    "fastest": 0.0,
    "comfortable": 0.25,
    "short_tacks": 0.5,
}


def _leg_cost(
    parent: IsochronePoint, heading_deg: float, env: Env, dt_hours: float, objective: str
) -> float:
    """Objective-specific cost increment for one leg (plan/04)."""
    if objective == "comfortable":
        wave_pen = 0.5 * (env.wave_height_m ** 2) * dt_hours
        over20 = max(0.0, env.wind_speed_kts - 20.0)
        wind_pen = 0.3 * over20 * dt_hours
        return wave_pen + wind_pen
    if objective == "short_tacks":
        if parent.heading_deg is None:
            return 0.0
        diff = abs(heading_deg - parent.heading_deg)
        diff = min(diff, 360.0 - diff)
        return 1.0 if diff > 60.0 else 0.0
    return 0.0


# --- sector pruning ---------------------------------------------------------


def sector_prune(
    points: list[IsochronePoint],
    destination: tuple[float, float],
    n_sectors: int = 72,
    objective: str = "fastest",
    min_frontier_floor: int | None = None,
) -> list[IsochronePoint]:
    """Keep one best point per angular sector around the centroid→destination axis.

    `half_width = 90°` — points making negative axial progress (bearing
    more than 90° off the axis) are rejected before bucketing. A wider
    acceptance window lets the frontier fan out north/south each step
    and eventually escape the forecast bbox in open-water transits.

    `min_frontier_floor` guarantees the returned frontier isn't smaller
    than `min(floor, |points|)` — when sectoring discards too much
    (narrow channel where only a thin wedge of propagations survived,
    or the 2-point edge case in the oblique-progress test), we top up
    with the remaining points nearest to the destination. Defaults to
    `min(20, n_sectors)`. This floor is what keeps obliquely-progressing
    points around when bucketing would otherwise drop them.
    """
    if not points:
        return []
    floor = min_frontier_floor if min_frontier_floor is not None else min(20, n_sectors)
    mean_lat = sum(p.lat for p in points) / len(points)
    mean_lon = sum(p.lon for p in points) / len(points)
    axis = bearing_deg(mean_lat, mean_lon, destination[0], destination[1])
    weight = _OBJECTIVE_WEIGHT.get(objective, 0.0)

    buckets: dict[int, tuple[float, IsochronePoint]] = {}
    half_width = 90.0
    sector_width = 2 * half_width / n_sectors

    for p in points:
        if p.lat == mean_lat and p.lon == mean_lon:
            continue
        b = bearing_deg(mean_lat, mean_lon, p.lat, p.lon)
        rel = ((b - axis + 540) % 360) - 180
        if abs(rel) > half_width:
            continue
        d = distance_nm(mean_lat, mean_lon, p.lat, p.lon)
        progress = d * cos(radians(rel))
        metric = progress - weight * p.accumulated_cost
        s = int((rel + half_width) / sector_width)
        cur = buckets.get(s)
        if cur is None or metric > cur[0]:
            buckets[s] = (metric, p)
    kept = [p for _, p in buckets.values()]

    if len(kept) < floor and len(points) > len(kept):
        kept_ids = {id(p) for p in kept}
        extras = sorted(
            (p for p in points if id(p) not in kept_ids),
            key=lambda p: distance_nm(p.lat, p.lon, destination[0], destination[1]),
        )
        kept.extend(extras[: floor - len(kept)])
    return kept


# --- motion step ------------------------------------------------------------


def _advance_with_current(
    lat: float, lon: float, heading: float, bsp_kts: float, env: Env, dt_hours: float
) -> tuple[float, float]:
    la, lo = advance(lat, lon, heading, bsp_kts * dt_hours)
    if env.current_speed_kts > 1e-6:
        la, lo = advance(la, lo, env.current_dir_deg, env.current_speed_kts * dt_hours)
    return la, lo


# --- main entry -------------------------------------------------------------


def plan_candidate(
    *,
    origin: tuple[float, float],
    destination: tuple[float, float],
    depart_at: datetime,
    polar: Polar,
    forecast: ForecastField,
    charts: ChartStore,
    boat: BoatLimits,
    objective: Objective = "fastest",
    step_minutes: int = 60,
    fine_step_minutes: int = 10,
    shore_threshold_nm: float = 3.0,
    max_steps: int = 168,
    arrival_tolerance_nm: float = 0.5,
) -> RouteResult:
    """Plan a single candidate departure.

    Graduated step size (plan/04 §Step schedule): when any point on
    the current frontier is within `shore_threshold_nm` of land, or
    within `shore_threshold_nm` of the destination, use
    `fine_step_minutes` — small enough to thread narrow channels
    without the straight-line segment between isochrone points
    jumping over land. Otherwise use the coarse `step_minutes`.
    The fine mode also swaps to a denser heading fan for better
    channel coverage.
    """
    coarse_delta = timedelta(minutes=step_minutes)
    fine_delta = timedelta(minutes=fine_step_minutes)
    coarse_h = step_minutes / 60.0
    fine_h = fine_step_minutes / 60.0

    origin_point = IsochronePoint(
        lat=origin[0], lon=origin[1], t=depart_at, parent=None,
        heading_deg=None, bsp_kts=0.0, env=None, accumulated_cost=0.0,
    )
    isochrones: list[list[IsochronePoint]] = [[origin_point]]
    no_coverage_frontier_count = 0
    t0 = time.monotonic()
    outcome_labels = {"objective": objective}
    current_t = depart_at

    # Pre-compute once whether each endpoint is itself near shore, so we
    # know whether we need fine stepping on the exit / approach.
    origin_near_shore = (
        charts.distance_to_land_nm(origin[0], origin[1]) <= shore_threshold_nm
    )
    destination_near_shore = (
        charts.distance_to_land_nm(destination[0], destination[1])
        <= shore_threshold_nm
    )
    # When exiting a cove, stay in fine mode until we're clear of the
    # origin's near-shore zone by this margin. `shore_threshold_nm * 2`
    # gives the boat room to get into genuinely open water before
    # switching to 60-min hops.
    exit_clear_nm = shore_threshold_nm * 2.0

    def _frontier_near_shore(pts: list[IsochronePoint]) -> bool:
        """Fine mode trigger evaluated against the lead edge only.

        - Looking at *any* frontier point keeps fine mode stuck on,
          since the wide fan always has an edge point within a few nm
          of some Chesapeake shoreline.
        - Looking at the *median* fails for the same reason in narrow
          bays — the typical path is always within a few nm of some
          coast.

        What actually matters: where is the boat's lead edge relative
        to the two shore-adjacent places that need careful stepping —
        the origin cove and the destination harbor? Everywhere in
        between, 60-min steps are fine even if the course grazes land.
        """
        if not pts:
            return False
        lead = min(
            pts,
            key=lambda p: distance_nm(
                p.lat, p.lon, destination[0], destination[1]
            ),
        )
        approach = destination_near_shore and distance_nm(
            lead.lat, lead.lon, destination[0], destination[1]
        ) <= shore_threshold_nm
        exit_ = origin_near_shore and distance_nm(
            lead.lat, lead.lon, origin[0], origin[1]
        ) <= exit_clear_nm
        return approach or exit_

    for step in range(1, max_steps + 1):
        frontier_pts = isochrones[-1]
        fine_mode = _frontier_near_shore(frontier_pts)
        step_h = fine_h if fine_mode else coarse_h
        step_delta = fine_delta if fine_mode else coarse_delta
        current_t = current_t + step_delta
        t = current_t
        frontier: list[IsochronePoint] = []
        fan_fn = heading_fan_fine if fine_mode else heading_fan

        for pt in frontier_pts:
            env = forecast.at(pt.lat, pt.lon, t)
            if env is None:
                continue
            if env.wind_speed_kts > boat.max_wind_kts or env.wave_height_m > boat.max_seas_m:
                continue

            course_to_dest = bearing_deg(pt.lat, pt.lon, destination[0], destination[1])
            for h in fan_fn(course_to_dest):
                twa = relative_wind_angle(h, env.wind_dir_deg)
                bsp = polar.bsp(twa, env.wind_speed_kts)
                if bsp < boat.min_bsp_kts:
                    continue

                new_lat, new_lon = _advance_with_current(pt.lat, pt.lon, h, bsp, env, step_h)

                if charts.crosses_land((pt.lat, pt.lon), (new_lat, new_lon)):
                    continue
                if charts.crosses_obstacle((pt.lat, pt.lon), (new_lat, new_lon)):
                    continue
                if charts.is_restricted((new_lat, new_lon)):
                    continue

                depth = charts.available_depth(new_lat, new_lon, t)
                if depth is None:
                    continue
                if depth < boat.draft_m + boat.min_depth_m:
                    continue

                new_cost = pt.accumulated_cost + _leg_cost(pt, h, env, step_h, objective)
                frontier.append(
                    IsochronePoint(
                        lat=new_lat, lon=new_lon, t=t, parent=pt,
                        heading_deg=h, bsp_kts=bsp, env=env,
                        accumulated_cost=new_cost,
                    )
                )

        _propagations_per_step.record(len(frontier), outcome_labels)

        if not frontier:
            no_coverage_frontier_count += 1
            if no_coverage_frontier_count >= 3:
                _steps.record(step, outcome_labels)
                _wallclock.record(time.monotonic() - t0, outcome_labels)
                _outcomes.add(1, {**outcome_labels, "outcome": "no_coverage"})
                raise RouterError("ROUTE_NO_COVERAGE", detail=f"empty frontier at step {step}")
            continue
        no_coverage_frontier_count = 0

        pruned = sector_prune(frontier, destination, objective=objective)
        isochrones.append(pruned)

        for p in pruned:
            if distance_nm(p.lat, p.lon, destination[0], destination[1]) <= arrival_tolerance_nm:
                final = IsochronePoint(
                    lat=destination[0], lon=destination[1], t=p.t,
                    parent=p, heading_deg=p.heading_deg, bsp_kts=p.bsp_kts, env=p.env,
                    accumulated_cost=p.accumulated_cost,
                )
                _steps.record(step, outcome_labels)
                _wallclock.record(time.monotonic() - t0, outcome_labels)
                _outcomes.add(1, {**outcome_labels, "outcome": "ok"})
                return RouteResult(
                    points=_backtrack(final),
                    reached_at=p.t,
                    steps_used=step,
                    objective=objective,
                    isochrones=isochrones,
                )

    _steps.record(max_steps, outcome_labels)
    _wallclock.record(time.monotonic() - t0, outcome_labels)
    _outcomes.add(1, {**outcome_labels, "outcome": "timeout"})
    raise RouterError("ROUTE_TIMEOUT", detail=f"exhausted {max_steps} steps")


def _backtrack(final: IsochronePoint) -> list[IsochronePoint]:
    out: list[IsochronePoint] = []
    cur: IsochronePoint | None = final
    while cur is not None:
        out.append(cur)
        cur = cur.parent
    out.reverse()
    return out
