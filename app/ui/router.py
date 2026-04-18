"""HTMX endpoints for the skipper UI.

Three surfaces:

- `GET /` — full HTML page (Leaflet + form + empty status slot).
- `POST /ui/voyages` — accepts form-encoded submission, calls the same
  `JobRegistry.submit` path the JSON API uses, returns the polling
  partial in HTML.
- `GET /ui/voyages/{id}/status` — polling target. Returns the running
  partial (with a trigger that keeps polling), the candidates partial
  (when `done`), or the error partial (when `failed` / terminal).
- `GET /ui/voyages/{id}/geojson` — parsed GPX for Leaflet rendering.
  Returns `{primary: [[lat,lon], ...], navaids: [...]}` for a single
  candidate rank so the browser can draw without re-parsing GPX.

The UI deliberately uses `?force=true` on every submit — a skipper
iterating on the window doesn't want 409s; they want the last thing
they asked for.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.logging import get_logger
from app.models.voyage import Voyage
from app.routers.voyages import _load as load_voyage
from app.schemas.request import Coord, TimeWindow, VoyageRequest
from app.services import boat_profiles
from app.services.jobs import LIVE_STAGES, delete_voyage, insert_voyage

log = get_logger(__name__)
router = APIRouter(tags=["ui"])

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

GPX_NS = "http://www.topografix.com/GPX/1/1"
BV_NS = "https://better-voyage.app/gpx/1"


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    profiles = await boat_profiles.list_all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "profiles": profiles,
            "default_profile": next(
                (p.name for p in profiles if p.name == "default"),
                profiles[0].name if profiles else "default",
            ),
        },
    )


# ---------------------------------------------------------------------------
# Voyage submission (form → HTMX partial)
# ---------------------------------------------------------------------------


@router.post("/ui/voyages", response_class=HTMLResponse)
async def submit_voyage(
    request: Request,
    origin_lat: float = Form(...),
    origin_lon: float = Form(...),
    destination_lat: float = Form(...),
    destination_lon: float = Form(...),
    origin_name: str = Form(""),
    destination_name: str = Form(""),
    start_at: str = Form(...),
    end_at: str = Form(...),
    tz: str = Form("UTC"),
    boat_profile_name: str = Form("default"),
    objective: str = Form("fastest"),
    max_candidates: int = Form(3),
    earliest_local: str = Form(""),
    latest_local: str = Form(""),
) -> Response:
    """Accept a form submission, spawn a voyage, return the polling partial.

    We always replace (`force=True`) because the UI is a single-slot
    workbench — there's no "preserve my earlier queued run" use case
    here, unlike API clients that might depend on dedupe semantics.
    """
    try:
        req = VoyageRequest(
            origin=Coord(
                lat=origin_lat, lon=origin_lon, name=origin_name or None
            ),
            destination=Coord(
                lat=destination_lat, lon=destination_lon,
                name=destination_name or None,
            ),
            window=TimeWindow(
                start_at=_parse_iso(start_at),
                end_at=_parse_iso(end_at),
                tz=tz,
                earliest_departure_local_time=_parse_local_time(earliest_local),
                latest_departure_local_time=_parse_local_time(latest_local),
            ),
            boat_profile_name=boat_profile_name,
            objective=objective,  # type: ignore[arg-type]
            max_candidates=max_candidates,
        )
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "_error.html",
            {"error_code": "INVALID_WINDOW", "detail": str(exc)[:300]},
            status_code=400,
        )

    if await boat_profiles.get(req.boat_profile_name) is None:
        return templates.TemplateResponse(
            request, "_error.html",
            {
                "error_code": "BOAT_PROFILE_NOT_FOUND",
                "detail": f"no boat profile named {req.boat_profile_name!r}",
            },
            status_code=404,
        )

    registry = request.app.state.registry
    # Single-slot UI: always replace the existing voyage.
    from app.services.jobs import find_existing

    existing = await find_existing()
    if existing is not None:
        if existing.status in LIVE_STAGES:
            await registry.cancel(existing.id)
        await delete_voyage(existing.id)

    vid = await insert_voyage(req)
    await registry.submit(vid)
    row = await load_voyage(vid)
    return templates.TemplateResponse(
        request, "_status.html", {"voyage": _row_view(row)}
    )


# ---------------------------------------------------------------------------
# Polling partial
# ---------------------------------------------------------------------------


@router.get(
    "/ui/voyages/{voyage_id}/status", response_class=HTMLResponse
)
async def voyage_status(voyage_id: str, request: Request) -> Response:
    row = await load_voyage(voyage_id)
    view = _row_view(row)
    if row.status == "done":
        view["candidates"] = _parse_candidates(row)
        view["navaids"] = _parse_navaids(row)
        return templates.TemplateResponse(
            request, "_candidates.html", {"voyage": view}
        )
    if row.status in {"failed", "cancelled"}:
        return templates.TemplateResponse(
            request, "_error.html",
            {
                "error_code": row.error_code or "FAILED",
                "detail": row.error_detail or row.status,
                "voyage_id": row.id,
            },
        )
    return templates.TemplateResponse(
        request, "_status.html", {"voyage": view}
    )


# ---------------------------------------------------------------------------
# GeoJSON for map rendering
# ---------------------------------------------------------------------------


@router.get("/ui/voyages/{voyage_id}/geojson")
async def voyage_geojson(voyage_id: str, candidate: int = 1) -> Response:
    row = await load_voyage(voyage_id)
    if row.status != "done" or row.gpx_blob is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "VOYAGE_NOT_READY", "status": row.status},
        )
    primary = _parse_candidate_linestring(row.gpx_blob, candidate)
    if primary is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "CANDIDATE_NOT_FOUND", "rank": candidate},
        )
    navaids = _parse_navaids(row)
    return JSONResponse({"primary": primary, "navaids": navaids})


# ---------------------------------------------------------------------------
# Helpers — GPX parsing
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    # `<input type="datetime-local">` returns "YYYY-MM-DDTHH:MM" without tz.
    # Python's fromisoformat accepts that; we coerce to UTC so the rest of
    # the app (which works entirely in aware UTC) stays consistent.
    from datetime import UTC

    s = s.strip()
    if not s:
        raise ValueError("missing datetime")
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _parse_local_time(s: str) -> time | None:
    s = s.strip()
    if not s:
        return None
    return time.fromisoformat(s)


def _row_view(row: Voyage) -> dict[str, Any]:
    try:
        progress = json.loads(row.progress_json or "{}")
    except json.JSONDecodeError:
        progress = {}
    try:
        coverage = json.loads(row.coverage_json or "null")
    except json.JSONDecodeError:
        coverage = None
    return {
        "id": row.id,
        "status": row.status,
        "stage": progress.get("stage", row.status),
        "pct": float(progress.get("pct") or 0.0),
        "detail": progress.get("detail"),
        "error_code": row.error_code,
        "error_detail": row.error_detail,
        "coverage": coverage,
    }


def _parse_candidates(row: Voyage) -> list[dict[str, Any]]:
    if not row.gpx_blob:
        return []
    try:
        root = ET.fromstring(row.gpx_blob)
    except ET.ParseError:
        return []
    out: list[dict[str, Any]] = []
    for rte in root.findall(f"{{{GPX_NS}}}rte"):
        if rte.findtext(f"{{{GPX_NS}}}type") == "escape_hatch_route":
            continue  # only surface primary candidates
        ext = rte.find(f"{{{GPX_NS}}}extensions")
        if ext is None:
            continue
        cand = ext.find(f"{{{BV_NS}}}candidate")
        if cand is None:
            continue
        rank_attr = cand.get("rank")
        if rank_attr is None:
            continue
        score_el = ext.find(f"{{{BV_NS}}}score")
        summary_el = ext.find(f"{{{BV_NS}}}summaryMd")
        out.append(
            {
                "rank": int(rank_attr),
                "depart_at": cand.get("departAt"),
                "arrive_at": cand.get("arriveAt"),
                "score": (
                    float(score_el.get("total", "0")) if score_el is not None
                    else None
                ),
                "summary": (
                    (summary_el.text or "").strip() if summary_el is not None
                    else None
                ),
                "summary_source": (
                    summary_el.get("source") if summary_el is not None else None
                ),
            }
        )
    out.sort(key=lambda c: c["rank"])
    return out


def _parse_navaids(row: Voyage) -> list[dict[str, Any]]:
    if not row.gpx_blob:
        return []
    try:
        root = ET.fromstring(row.gpx_blob)
    except ET.ParseError:
        return []
    out: list[dict[str, Any]] = []
    for wpt in root.findall(f"{{{GPX_NS}}}wpt"):
        lat = wpt.get("lat")
        lon = wpt.get("lon")
        if lat is None or lon is None:
            continue
        out.append(
            {
                "lat": float(lat),
                "lon": float(lon),
                "name": wpt.findtext(f"{{{GPX_NS}}}name") or "",
                "sym": wpt.findtext(f"{{{GPX_NS}}}sym") or "",
                "desc": wpt.findtext(f"{{{GPX_NS}}}desc") or "",
            }
        )
    return out


def _parse_candidate_linestring(
    blob: bytes, rank: int
) -> list[list[float]] | None:
    """Extract `[[lat, lon], ...]` for the given candidate's primary rte."""
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return None
    for rte in root.findall(f"{{{GPX_NS}}}rte"):
        if rte.findtext(f"{{{GPX_NS}}}type") == "escape_hatch_route":
            continue
        ext = rte.find(f"{{{GPX_NS}}}extensions")
        cand = (
            ext.find(f"{{{BV_NS}}}candidate") if ext is not None else None
        )
        if cand is None or cand.get("rank") != str(rank):
            continue
        points: list[list[float]] = []
        for rtept in rte.findall(f"{{{GPX_NS}}}rtept"):
            try:
                points.append(
                    [float(rtept.get("lat", "0")), float(rtept.get("lon", "0"))]
                )
            except (TypeError, ValueError):
                continue
        if points:
            return points
    return None


__all__ = ["router"]
