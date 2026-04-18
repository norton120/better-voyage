"""GPX emission via gpxpy.

Replaces the hand-rolled string builder that shipped in M2. The tree
is built with `gpxpy.gpx.GPX` / `GPXRoute` / `GPXRoutePoint` and
round-trip-safe `bv:` extensions are appended as `xml.etree.Element`
instances — gpxpy preserves them on both `to_xml` and `parse`.

Ordering follows plan/09 §Deterministic element order:

1. `<metadata>` (with `<bv:coverage>` inside `<extensions>`).
2. `<rte>` by `bv:candidate/@rank` ascending; each candidate's
   escape-hatch `<rte>` elements follow their parent immediately.

Foreign namespaces (OpenCPN's `opencpn:viz`, Garmin's `gpxx:*`, etc.)
are preserved because gpxpy keeps unknown extension children as
`ET.Element` verbatim.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import gpxpy
import gpxpy.gpx

from app.config import get_settings

if TYPE_CHECKING:
    from app.services.charts import Waypoint as ChartWaypoint
    from app.services.contingency import (
        BackupDestination,
        EscapeHatch,
        TapOut,
    )
    from app.services.planner import Candidate, PlanState
    from app.services.router import IsochronePoint
    from app.services.summary import Summary

BV_NS = "https://better-voyage.app/gpx/1"
_CREATOR = "better-voyage/0.1"


def _bv(tag: str, attrs: dict[str, str] | None = None) -> ET.Element:
    el = ET.Element(f"{{{BV_NS}}}{tag}")
    if attrs:
        for k, v in attrs.items():
            el.set(k, v)
    return el


def emit_voyage(state: PlanState) -> bytes:
    """Serialize the planner's final state to a GPX 1.1 byte string.

    Shape is fixed by plan/09 / plan/06; ordering by plan/09. The
    output validates as well-formed XML and carries the `bv:` namespace
    so OpenCPN leaves unknown children alone.
    """
    gpx = gpxpy.gpx.GPX()
    gpx.creator = _CREATOR
    gpx.nsmap = {"bv": BV_NS}
    # Setting `name` triggers `<metadata>` rendering by gpxpy; extensions
    # riding on metadata only serialize when at least one other metadata
    # field is populated.
    gpx.name = "voyage"

    cov = _coverage_element(state)
    if cov is not None:
        gpx.metadata_extensions.append(cov)

    for c in state.candidates:
        gpx.routes.append(_candidate_route(state, c))
        for h in c.escape_hatches:
            gpx.routes.append(_escape_hatch_route(c, h))

    for wp in _navaids_for_routes(state):
        gpx.waypoints.append(wp)

    return gpx.to_xml(version="1.1", prettyprint=True).encode("utf-8")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _coverage_element(state: PlanState) -> ET.Element | None:
    """Per plan/15 §Coverage block + plan/10 §voyage.bv:coverage.charts."""
    attrs: dict[str, str] = {}
    if state.forecast_stale_at is not None:
        attrs["forecastStaleAt"] = state.forecast_stale_at.isoformat()
    cov = state.charts_coverage
    if cov is not None:
        attrs["encCells"] = str(cov.enc_cells)
        attrs["osmExtracts"] = str(cov.osm_extracts)
        if cov.gebco_tile is not None:
            attrs["gebcoTile"] = cov.gebco_tile
        if cov.fetched_at is not None:
            attrs["fetchedAt"] = cov.fetched_at.isoformat()
        attrs["tideModulatedDepth"] = "true" if cov.tide_modulated_depth else "false"
    if not attrs:
        return None
    return _bv("coverage", attrs)


def _navaids_for_routes(state: PlanState) -> list[gpxpy.gpx.GPXWaypoint]:
    """Emit `<wpt>` for navaids within `BV_NAVAID_BBOX_PAD_NM` of any leg.

    Delegates to `ChartStore.navaids_in(bbox)` — the bbox is the padded
    envelope of every rtept across every candidate primary route.
    Escape-hatch routes are excluded since their waypoints already live
    near the primary's navaid set.
    """
    store = state.charts
    if store is None or not state.candidates:
        return []
    try:
        navaids_in = store.navaids_in  # Protocol duck-type
    except AttributeError:
        return []
    all_pts = [pt for c in state.candidates for pt in c.route.points]
    if not all_pts:
        return []
    lat_min = min(pt.lat for pt in all_pts)
    lat_max = max(pt.lat for pt in all_pts)
    lon_min = min(pt.lon for pt in all_pts)
    lon_max = max(pt.lon for pt in all_pts)
    # Convert BV_NAVAID_BBOX_PAD_NM to degrees. 1 nm ≈ 1/60 deg lat; lon
    # scale varies with latitude but for the padding envelope rough is fine.
    pad_nm = get_settings().navaid_bbox_pad_nm
    pad_deg = pad_nm / 60.0
    bbox = (
        lat_min - pad_deg,
        lon_min - pad_deg,
        lat_max + pad_deg,
        lon_max + pad_deg,
    )
    seen: set[tuple[float, float]] = set()
    out: list[gpxpy.gpx.GPXWaypoint] = []
    for n in navaids_in(bbox):
        key = (round(n.lat, 6), round(n.lon, 6))
        if key in seen:
            continue
        seen.add(key)
        out.append(_navaid_waypoint(n))
    return out


def _navaid_waypoint(n: ChartWaypoint) -> gpxpy.gpx.GPXWaypoint:
    wp = gpxpy.gpx.GPXWaypoint(latitude=n.lat, longitude=n.lon)
    if n.name:
        wp.name = n.name
    if n.sym:
        wp.symbol = n.sym
    if n.desc:
        wp.description = n.desc
    return wp


def _candidate_route(state: PlanState, c: Candidate) -> gpxpy.gpx.GPXRoute:
    origin_label = state.req.origin.name or "Origin"
    dest_label = state.req.destination.name or "Destination"

    rte = gpxpy.gpx.GPXRoute()
    rte.name = f"Candidate {c.rank}: {origin_label} -> {dest_label}"
    rte.type = "primary"
    rte.extensions.append(
        _bv(
            "candidate",
            {
                "rank": str(c.rank),
                "departAt": c.depart_at.isoformat(),
                "arriveAt": c.route.reached_at.isoformat(),
            },
        )
    )
    rte.extensions.append(_bv("score", {"total": f"{c.score.total:.2f}"}))
    if c.summary is not None:
        rte.extensions.append(_summary_element(c.summary))
    if c.backup_destinations:
        rte.extensions.append(_backup_destinations_element(c.backup_destinations))

    pts = c.route.points
    for idx, p in enumerate(pts):
        if idx == 0:
            rte.points.append(_rtept(p, name=origin_label))
        elif idx == len(pts) - 1:
            rte.points.append(_rtept(p, name=dest_label))
        else:
            rte.points.append(_rtept(p, tapouts=c.tapouts_by_index.get(idx)))
    return rte


def _escape_hatch_route(c: Candidate, h: EscapeHatch) -> gpxpy.gpx.GPXRoute:
    rte = gpxpy.gpx.GPXRoute()
    rte.name = f"Candidate {c.rank} — {h.description}"
    rte.type = "escape_hatch_route"
    rte.extensions.append(_bv("candidateRank", {"value": str(c.rank)}))
    rte.extensions.append(_bv("contingencyKind", {"value": "escape_hatch_route"}))
    rte.extensions.append(_bv("parentRtept", {"index": str(h.parent_rtept_index)}))
    rte.extensions.append(
        _bv("trigger", {k: _fmt_float(v) for k, v in h.trigger.items()})
    )
    pts = h.route.points
    for idx, p in enumerate(pts):
        if idx == 0:
            rte.points.append(_rtept(p, name="Escape start"))
        elif idx == len(pts) - 1:
            rte.points.append(_rtept(p, name=h.target_name))
        else:
            rte.points.append(_rtept(p))
    return rte


def _rtept(
    p: IsochronePoint,
    *,
    name: str = "",
    tapouts: list[TapOut] | None = None,
) -> gpxpy.gpx.GPXRoutePoint:
    pt = gpxpy.gpx.GPXRoutePoint(latitude=p.lat, longitude=p.lon)
    pt.time = p.t
    if name:
        pt.name = name
    exts: list[ET.Element] = []
    if p.heading_deg is not None and p.bsp_kts > 0:
        exts.append(
            _bv(
                "leg",
                {
                    "bearingDeg": f"{p.heading_deg:.1f}",
                    "bspKts": f"{p.bsp_kts:.2f}",
                },
            )
        )
    if tapouts:
        exts.append(_tapout_element(tapouts))
    pt.extensions.extend(exts)
    return pt


def _tapout_element(tapouts: list[TapOut]) -> ET.Element:
    node = _bv("tapOut")
    for t in tapouts:
        node.append(
            _bv(
                "option",
                {
                    "name": t.name,
                    "lat": f"{t.lat:.6f}",
                    "lon": f"{t.lon:.6f}",
                    "detourNm": f"{t.detour_nm:.2f}",
                    "type": t.type or "",
                },
            )
        )
    return node


def _backup_destinations_element(
    backups: list[BackupDestination],
) -> ET.Element:
    node = _bv("backupDestinations")
    for b in backups:
        node.append(
            _bv(
                "option",
                {
                    "name": b.name,
                    "lat": f"{b.lat:.6f}",
                    "lon": f"{b.lon:.6f}",
                    "detourNm": f"{b.detour_nm:.2f}",
                },
            )
        )
    return node


def _summary_element(summary: Summary) -> ET.Element:
    node = _bv("summaryMd", {"source": summary.source})
    node.text = summary.text
    return node


def _fmt_float(v: float | int | str) -> str:
    if isinstance(v, str):
        return v
    return f"{float(v):.2f}"


__all__ = ["BV_NS", "emit_voyage"]
