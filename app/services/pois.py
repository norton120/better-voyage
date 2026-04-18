"""POI ingest and in-memory spatial queries.

Per plan/03 §POIs + plan/11 §POIs, POIs live in a GPX file at
`app/data/pois.gpx`, hand-curated in OpenCPN and committed to the
repo. Supplementary files under `BV_POI_DIRS` merge at startup —
drop-in path for OpenSeaMap extracts, Active Captain exports, etc.

For MVP we keep POIs purely in memory: load at startup, filter
linearly on bbox / sym / type. Replacing the naive scan with a
shapely STRtree lands with ChartStore (plan/15) which already needs
the spatial index.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import gpxpy

from app.logging import get_logger

log = get_logger(__name__)

DEFAULT_POI_PATH = Path(__file__).parent.parent / "data" / "pois.gpx"


@dataclass
class POI:
    lat: float
    lon: float
    name: str | None = None
    sym: str | None = None
    type: str | None = None
    desc: str | None = None
    extras: dict[str, str] = field(default_factory=dict)


_cache: list[POI] | None = None


def _parse_gpx(path: Path) -> list[POI]:
    out: list[POI] = []
    with path.open(encoding="utf-8") as f:
        gpx = gpxpy.parse(f)
    for w in gpx.waypoints:
        extras: dict[str, str] = {}
        if w.extensions:
            for el in w.extensions:
                tag = getattr(el, "tag", "")
                if isinstance(tag, str) and "}" in tag:
                    tag = tag.split("}", 1)[1]
                text = getattr(el, "text", None)
                if text:
                    extras[str(tag)] = text.strip()
        out.append(
            POI(
                lat=w.latitude,
                lon=w.longitude,
                name=w.name,
                sym=w.symbol,
                type=w.type,
                desc=w.description,
                extras=extras,
            )
        )
    return out


def _extra_dirs() -> list[Path]:
    raw = os.getenv("BV_POI_DIRS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p]


def load_all(force: bool = False) -> list[POI]:
    """Load `app/data/pois.gpx` plus any `BV_POI_DIRS` extras.

    Cached across calls. Pass `force=True` to re-read (used in tests
    and, later, a SIGHUP handler).
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    pois: list[POI] = []
    if DEFAULT_POI_PATH.exists():
        pois.extend(_parse_gpx(DEFAULT_POI_PATH))
    for d in _extra_dirs():
        if not d.exists():
            continue
        for f in sorted(d.glob("*.gpx")):
            try:
                pois.extend(_parse_gpx(f))
            except Exception:
                log.warning("pois.parse_failed", path=str(f))
    log.info("pois.loaded", count=len(pois), source=str(DEFAULT_POI_PATH))
    _cache = pois
    return pois


def query(
    bbox: tuple[float, float, float, float] | None = None,
    syms: set[str] | None = None,
    types: set[str] | None = None,
) -> list[POI]:
    """Filter loaded POIs. bbox = (min_lon, min_lat, max_lon, max_lat)."""
    out: list[POI] = []
    for p in load_all():
        if bbox is not None:
            min_lon, min_lat, max_lon, max_lat = bbox
            if not (min_lat <= p.lat <= max_lat):
                continue
            if not (min_lon <= p.lon <= max_lon):
                continue
        if syms and p.sym not in syms:
            continue
        if types and p.type not in types:
            continue
        out.append(p)
    return out
