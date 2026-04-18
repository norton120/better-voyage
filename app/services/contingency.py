"""Contingency annotations + escape-hatch re-routes.

For M4 we emit three kinds of contingency artifacts (plan/06):

1. **Backup destinations** — POI extensions on the terminal rtept.
2. **Tap-outs** — POI annotations on decision-point rtepts.
3. **Escape-hatch routes** — isochrone re-routes from a decision
   point to a nearby refuge, emitted as sibling `<rte>` elements
   when the downstream segment env exceeds thresholds AND the
   re-route diverges meaningfully from the primary (discrete
   Fréchet > `ESCAPE_DIVERGENCE_NM`).

Thresholds per plan/06 §Thresholds. All distances in nautical miles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from app.logging import get_logger
from app.observability import meter
from app.services.geo import discrete_frechet_nm, distance_nm
from app.services.pois import POI
from app.services.pois import query as query_pois
from app.services.router import (
    BoatLimits,
    IsochronePoint,
    RouteResult,
    RouterError,
    plan_candidate,
)

if TYPE_CHECKING:
    from app.services.charts import ChartStore
    from app.services.forecast_field import ForecastField
    from app.services.polars import Polar

log = get_logger(__name__)
_m = meter("app.services.contingency")
_emitted = _m.create_counter(
    "bv.contingencies.emitted",
    description="Contingency artifacts emitted per voyage, labeled by kind",
    unit="1",
)

BACKUP_RADIUS_NM = 5.0
TAPOUT_DETOUR_NM = 8.0
TAPOUT_KEEP_TOP_N = 3

ESCAPE_SEAS_M = 2.0
ESCAPE_WIND_KTS = 25.0
ESCAPE_DIVERGENCE_NM = 2.0
ESCAPE_REFUGE_RADIUS_NM = 30.0

DECISION_POINT_INTERVAL_H = 4.0

_SAFE_HARBOR_TYPES = {"marina", "anchorage", "harbor_of_refuge"}
_REFUGE_TYPES = {"harbor_of_refuge", "anchorage"}


@dataclass(frozen=True)
class BackupDestination:
    name: str
    lat: float
    lon: float
    detour_nm: float


@dataclass(frozen=True)
class TapOut:
    name: str
    lat: float
    lon: float
    detour_nm: float
    type: str | None
    sym: str | None


def _nearby_safe_harbors(
    lat: float, lon: float, radius_nm: float
) -> list[tuple[POI, float]]:
    """Every safe-harbor POI within `radius_nm`, sorted by distance."""
    pad_deg = radius_nm / 45.0  # rough degrees per nm at midlatitudes
    bbox = (lon - pad_deg, lat - pad_deg, lon + pad_deg, lat + pad_deg)
    pois = query_pois(bbox=bbox, types=_SAFE_HARBOR_TYPES)
    scored: list[tuple[POI, float]] = []
    for p in pois:
        d = distance_nm(lat, lon, p.lat, p.lon)
        if d <= radius_nm:
            scored.append((p, d))
    scored.sort(key=lambda x: x[1])
    return scored


def find_backup_destinations(
    destination_lat: float, destination_lon: float
) -> list[BackupDestination]:
    out = [
        BackupDestination(
            name=p.name or "POI",
            lat=p.lat,
            lon=p.lon,
            detour_nm=round(d, 2),
        )
        for p, d in _nearby_safe_harbors(destination_lat, destination_lon, BACKUP_RADIUS_NM)
        # Drop the destination itself (zero-distance match when origin/POI coincide).
        if d > 0.01
    ]
    if out:
        _emitted.add(len(out), {"kind": "backup_destination"})
    return out


def decision_points(
    route_points: list[IsochronePoint], every_hours: float = DECISION_POINT_INTERVAL_H
) -> list[IsochronePoint]:
    """Pick decision-point rtepts every `every_hours` of elapsed passage
    time. Always includes the first intermediate point, never the origin
    or destination (those have their own contingency treatment)."""
    if len(route_points) < 3:
        return []
    start_t = route_points[0].t
    next_mark = start_t + timedelta(hours=every_hours)
    picks: list[IsochronePoint] = []
    # Skip origin (index 0) and destination (index -1).
    for p in route_points[1:-1]:
        if p.t >= next_mark:
            picks.append(p)
            next_mark = p.t + timedelta(hours=every_hours)
    return picks


def find_tapouts(point: IsochronePoint) -> list[TapOut]:
    out = [
        TapOut(
            name=p.name or "POI",
            lat=p.lat,
            lon=p.lon,
            detour_nm=round(d, 2),
            type=p.type,
            sym=p.sym,
        )
        for p, d in _nearby_safe_harbors(point.lat, point.lon, TAPOUT_DETOUR_NM)[
            :TAPOUT_KEEP_TOP_N
        ]
    ]
    if out:
        _emitted.add(len(out), {"kind": "tap_out"})
    return out


# ---------------------------------------------------------------------------
# Escape-hatch routes (plan/06 §3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscapeHatch:
    """An alternate route from a decision-point rtept to a nearby refuge,
    generated when the downstream segment's env violates a threshold."""

    route: RouteResult
    trigger: dict[str, float]
    parent_rtept_index: int
    target_name: str
    target_lat: float
    target_lon: float

    @property
    def description(self) -> str:
        parts: list[str] = []
        if "seas_m_gt" in self.trigger:
            parts.append(f"seas > {self.trigger['seas_m_gt']:.1f} m")
        if "wind_kts_gt" in self.trigger:
            parts.append(f"wind > {self.trigger['wind_kts_gt']:.0f} kt")
        return f"escape to {self.target_name} ({', '.join(parts) or 'risk trigger'})"


def _nearest_refuge(lat: float, lon: float) -> POI | None:
    """Closest `harbor_of_refuge` or `anchorage` POI within
    `ESCAPE_REFUGE_RADIUS_NM`, or `None` if nothing qualifies."""
    pad_deg = ESCAPE_REFUGE_RADIUS_NM / 45.0
    bbox = (lon - pad_deg, lat - pad_deg, lon + pad_deg, lat + pad_deg)
    pois = query_pois(bbox=bbox, types=_REFUGE_TYPES)
    candidates: list[tuple[POI, float]] = []
    for p in pois:
        d = distance_nm(lat, lon, p.lat, p.lon)
        if 0.5 < d <= ESCAPE_REFUGE_RADIUS_NM:
            candidates.append((p, d))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def _env_trigger(
    decision: IsochronePoint, downstream: list[IsochronePoint]
) -> dict[str, float] | None:
    """Return the trigger (as a `{seas_m_gt, wind_kts_gt}` dict of the
    actual offending readings) if any downstream leg violates an escape
    threshold, else `None`.

    "Downstream" means the rtepts chronologically after `decision` on
    the same route, including the destination.
    """
    max_seas = 0.0
    max_wind = 0.0
    for p in downstream:
        if p.env is None:
            continue
        max_seas = max(max_seas, p.env.wave_height_m)
        max_wind = max(max_wind, p.env.wind_speed_kts)
    trigger: dict[str, float] = {}
    if max_seas > ESCAPE_SEAS_M:
        trigger["seas_m_gt"] = round(max_seas, 2)
    if max_wind > ESCAPE_WIND_KTS:
        trigger["wind_kts_gt"] = round(max_wind, 1)
    return trigger or None


def _tightened(boat: BoatLimits, trigger: dict[str, float]) -> BoatLimits:
    """Tighten hard limits for the re-route: `max_seas_m` reduced by
    0.5 m below the trigger value (or the original limit, whichever
    is stricter). Wind limit unchanged — the refuge detour itself
    carries the wind penalty via the polar."""
    seas = boat.max_seas_m
    if "seas_m_gt" in trigger:
        seas = min(seas, max(0.5, trigger["seas_m_gt"] - 0.5))
    return BoatLimits(
        draft_m=boat.draft_m,
        min_depth_m=boat.min_depth_m,
        max_wind_kts=boat.max_wind_kts,
        max_seas_m=seas,
        min_bsp_kts=boat.min_bsp_kts,
    )


def _decimate(points: list[IsochronePoint]) -> list[tuple[float, float]]:
    """Downsample to ≤32 points before Fréchet — cost is O(n·m) and
    we don't need per-rtept precision to call the divergence."""
    if len(points) <= 32:
        return [(p.lat, p.lon) for p in points]
    step = max(1, len(points) // 32)
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        sampled = sampled + [points[-1]]
    return [(p.lat, p.lon) for p in sampled]


def _meaningfully_different(
    alt: list[IsochronePoint], primary_tail: list[IsochronePoint]
) -> bool:
    """True when the escape route's decimated path diverges from the
    primary tail by > `ESCAPE_DIVERGENCE_NM` under discrete Fréchet."""
    d = discrete_frechet_nm(_decimate(alt), _decimate(primary_tail))
    return d > ESCAPE_DIVERGENCE_NM


def plan_escape_hatches(
    *,
    primary: RouteResult,
    decision_indices: list[int],
    boat: BoatLimits,
    forecast: ForecastField,
    polar: Polar,
    charts: ChartStore,
    objective: str = "fastest",
) -> list[EscapeHatch]:
    """For each decision-point index on the primary route, emit an
    `EscapeHatch` if:

    - the downstream env crosses a threshold, AND
    - a refuge POI exists within `ESCAPE_REFUGE_RADIUS_NM`, AND
    - the re-router converges from the decision point, AND
    - the alternate path differs meaningfully from the primary tail.

    Swallows `RouterError` silently — a missing escape route is
    expected when no alternate converges, not a voyage-level failure.
    """
    hatches: list[EscapeHatch] = []
    points = primary.points
    for idx in decision_indices:
        if idx <= 0 or idx >= len(points) - 1:
            continue
        decision = points[idx]
        downstream = points[idx + 1 :]
        trigger = _env_trigger(decision, downstream)
        if trigger is None:
            continue
        refuge = _nearest_refuge(decision.lat, decision.lon)
        if refuge is None:
            continue
        try:
            alt = plan_candidate(
                origin=(decision.lat, decision.lon),
                destination=(refuge.lat, refuge.lon),
                depart_at=decision.t,
                polar=polar,
                forecast=forecast,
                charts=charts,
                boat=_tightened(boat, trigger),
                objective=objective,
                step_minutes=60,
                max_steps=48,
                arrival_tolerance_nm=0.5,
            )
        except RouterError as exc:
            log.info(
                "contingency.escape_hatch.skipped",
                reason=exc.code,
                parent_idx=idx,
                refuge=refuge.name,
            )
            continue
        if not _meaningfully_different(alt.points, downstream):
            continue
        hatches.append(
            EscapeHatch(
                route=alt,
                trigger=trigger,
                parent_rtept_index=idx,
                target_name=refuge.name or "refuge",
                target_lat=refuge.lat,
                target_lon=refuge.lon,
            )
        )
    if hatches:
        _emitted.add(len(hatches), {"kind": "escape_hatch_route"})
    return hatches
