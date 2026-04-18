"""GET /pois — list POIs by bbox + optional sym / type filters.

Plan/10 §GET /pois. The response shape mirrors GPX `<wpt>` per
plan/01 §Waypoint with a small subset surfaced for API consumers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services import pois as pois_svc

router = APIRouter(prefix="/pois", tags=["pois"])


def _parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = raw.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_BBOX", "detail": "expected minLon,minLat,maxLon,maxLat"},
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(x) for x in parts)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_BBOX", "detail": str(exc)},
        ) from exc
    if min_lat > max_lat or min_lon > max_lon:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_BBOX", "detail": "min must be <= max"},
        )
    return min_lon, min_lat, max_lon, max_lat


def _csv_set(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    return {s.strip() for s in raw.split(",") if s.strip()}


@router.get("", summary="List POIs in a bbox")
async def list_pois(
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
    sym: str | None = Query(None, description="Comma-separated GPX sym values"),
    type: str | None = Query(None, description="Comma-separated type values"),
) -> list[dict[str, Any]]:
    pois = pois_svc.query(
        bbox=_parse_bbox(bbox),
        syms=_csv_set(sym),
        types=_csv_set(type),
    )
    return [
        {
            "lat": p.lat,
            "lon": p.lon,
            "name": p.name,
            "sym": p.sym,
            "type": p.type,
            "desc": p.desc,
            "extensions": {"bv": p.extras} if p.extras else {},
        }
        for p in pois
    ]
