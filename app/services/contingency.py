"""Contingency annotations (backup anchorages, tap-outs).

For M4's first slice we emit **annotations only** — extension fields
on the primary `<rte>` and its decision-point rtepts. No new `<rte>`
elements. The escape-hatch re-router (plan/06 §3) that spawns
alternate isochrone runs lands next.

Thresholds per plan/06 §Thresholds. All distances in nautical miles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.services.geo import distance_nm
from app.services.pois import POI
from app.services.pois import query as query_pois
from app.services.router import IsochronePoint

BACKUP_RADIUS_NM = 5.0
TAPOUT_DETOUR_NM = 8.0
TAPOUT_KEEP_TOP_N = 3

DECISION_POINT_INTERVAL_H = 4.0

_SAFE_HARBOR_TYPES = {"marina", "anchorage", "harbor_of_refuge"}


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
    return [
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
    return [
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
