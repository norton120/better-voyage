"""OpenSeaMap (OSM) preprocessor.

OpenSeaMap is an OSM-derived marine dataset that provides global
coastline and seamark data (buoys, beacons, lights, wrecks, restricted
areas, etc.). This module reads an OSM PBF or XML extract and emits a
single GeoJSON FeatureCollection whose features carry a `bv:layer` tag
drawn from `charts_schema.Layer`. The ChartStore then consumes the
preprocessed GeoJSON and unions extracts into in-memory STRtrees.

Usage:

    meta = preprocess_osm_extract(
        pbf_path=Path("chesapeake.osm.pbf"),
        bbox=(36.5, -77.0, 39.5, -75.0),
        out_path=Path("chesapeake.preprocessed.geojson"),
    )

The preprocessor is deliberately narrow in scope — plan/15 §OpenSeaMap
lifts only four layers out of OSM:

- `land`      — `natural=coastline` ways (LineStrings).
- `obstacle`  — `seamark:type` in {wreck, rock, obstruction}.
- `restricted`— `seamark:type` in {restricted_area, marine_farm}.
- `navaid`    — `seamark:type` matching buoy_.* / beacon_.* / light_.*
                with the symbol pre-mapped to an OpenCPN sym name.

OSM has no DEPARE equivalent; depth comes from GEBCO and (in US waters)
NOAA ENC — the `shallow` layer is never populated here.

Bbox filtering is applied post-parse via shapely intersection — simpler
than osmium bbox pre-filtering and keeps the code portable across
.osm / .osm.pbf / .osm.bz2 inputs. For city-scale extracts the overhead
is negligible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import osmium
import osmium.geom
from shapely.geometry import LineString, Point, Polygon, box, mapping

from app.observability import meter, tracer
from app.services.charts_schema import LAYER_KEY, NAVAID_SYMS

Bbox = tuple[float, float, float, float]  # lat_min, lon_min, lat_max, lon_max


_tracer = tracer("app.services.charts_osm")
_m = meter("app.services.charts_osm")
_cells_loaded = _m.create_counter(
    "bv.charts.cells_loaded",
    description="OSM extracts preprocessed into GeoJSON",
    unit="1",
)


@dataclass(frozen=True)
class OsmExtractMeta:
    """Metadata describing a preprocessed OSM extract.

    `extract_id` is the input file stem (e.g. `chesapeake` for
    `chesapeake.osm.pbf`). `fetched_at` is the file's mtime if
    discoverable — the preprocessor itself doesn't touch the network.
    `feature_counts` is a per-layer count of features in the output.
    """

    extract_id: str
    bbox: Bbox
    fetched_at: datetime | None
    feature_counts: dict[str, int]


# Seamark groups used for classification. Kept module-level so tests
# can import them if needed.
_OBSTACLE_TYPES = frozenset({"wreck", "rock", "obstruction"})
_RESTRICTED_TYPES = frozenset({"restricted_area", "marine_farm"})
_NAVAID_PREFIXES = ("buoy_", "beacon_", "light_")

# Cardinal buoys carry a color top-mark. N/S quadrants are all-black-over-
# yellow or yellow-over-black; E/W mix both. For navaid classification
# on a GPX chartplotter all that matters is "it's a cardinal marker" so
# we drop them all into the yellow bucket.
_CARDINAL_QUADRANTS = frozenset({"north", "east", "south", "west"})


def preprocess_osm_extract(
    pbf_path: Path,
    bbox: Bbox,
    out_path: Path,
) -> OsmExtractMeta:
    """Read an OSM PBF/XML, filter to bbox, write a layer-tagged GeoJSON.

    `bbox` is `(lat_min, lon_min, lat_max, lon_max)`. The output is a
    single GeoJSON FeatureCollection at `out_path`; each feature carries
    `properties["bv:layer"]` drawn from `charts_schema.LAYERS`. The
    GeoJSON top-level `bbox` is standard order
    `[lon_min, lat_min, lon_max, lat_max]`.

    Raises `FileNotFoundError` if `pbf_path` does not exist.
    """
    if not pbf_path.exists():
        raise FileNotFoundError(f"OSM extract not found: {pbf_path}")

    extract_id = pbf_path.name
    # Strip all OSM-ish suffixes (`.osm`, `.pbf`, `.bz2`, `.osm.pbf`).
    for suf in (".pbf", ".bz2", ".osm", ".xml"):
        if extract_id.endswith(suf):
            extract_id = extract_id[: -len(suf)]

    with _tracer.start_as_current_span(
        "charts.preprocess",
        attributes={
            "charts.source": "osm",
            "charts.extract_id": extract_id,
            "charts.path": str(pbf_path),
        },
    ) as span:
        handler = _OsmLifter(bbox)
        handler.apply_file(str(pbf_path), locations=True, idx="flex_mem")

        features = handler.features
        feature_counts: dict[str, int] = {}
        for feat in features:
            layer = feat["properties"][LAYER_KEY]
            feature_counts[layer] = feature_counts.get(layer, 0) + 1

        lat_min, lon_min, lat_max, lon_max = bbox
        fc = {
            "type": "FeatureCollection",
            "bbox": [lon_min, lat_min, lon_max, lat_max],
            "features": features,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fp:
            json.dump(fc, fp, separators=(",", ":"))

        fetched_at: datetime | None = None
        try:
            fetched_at = datetime.fromtimestamp(pbf_path.stat().st_mtime)
        except OSError:
            fetched_at = None

        span.set_attribute("charts.feature_count", len(features))
        for layer, n in feature_counts.items():
            span.set_attribute(f"charts.features.{layer}", n)
        _cells_loaded.add(1, {"source": "osm"})

        return OsmExtractMeta(
            extract_id=extract_id,
            bbox=bbox,
            fetched_at=fetched_at,
            feature_counts=feature_counts,
        )


class _OsmLifter(osmium.SimpleHandler):
    """Lifts OSM features into bv-layer-tagged GeoJSON features.

    Uses osmium's flex-mem node-location index (via `apply_file(...,
    locations=True)`) so ways carry resolved lat/lon for each node ref.
    Geometry construction goes through shapely rather than osmium's WKB
    factory — simpler error handling, and matches how the ChartStore
    will consume the output.
    """

    def __init__(self, bbox: Bbox) -> None:
        super().__init__()
        self._bbox = bbox
        # shapely box() wants (minx, miny, maxx, maxy) = lon/lat order.
        lat_min, lon_min, lat_max, lon_max = bbox
        self._bbox_poly = box(lon_min, lat_min, lon_max, lat_max)
        self.features: list[dict[str, Any]] = []

    def node(self, n: osmium.osm.Node) -> None:  # type: ignore[override]
        if not n.location.valid():
            return
        lat, lon = n.location.lat, n.location.lon
        tags = _tags_to_dict(n.tags)
        seamark = tags.get("seamark:type")
        if seamark is None:
            return
        if not self._bbox_poly.covers(Point(lon, lat)):
            return

        if seamark in _OBSTACLE_TYPES:
            self.features.append(
                _make_feature(
                    geom=Point(lon, lat),
                    layer="obstacle",
                    props={
                        "osm_id": f"node/{n.id}",
                        "seamark:type": seamark,
                        "name": tags.get("seamark:name") or tags.get("name"),
                        "clearance_m": _parse_clearance(tags, seamark),
                    },
                )
            )
            return

        if any(seamark.startswith(p) for p in _NAVAID_PREFIXES):
            sym = _seamark_to_sym(tags)
            self.features.append(
                _make_feature(
                    geom=Point(lon, lat),
                    layer="navaid",
                    props={
                        "osm_id": f"node/{n.id}",
                        "seamark:type": seamark,
                        "sym": sym,
                        "name": tags.get("seamark:name") or tags.get("name"),
                        "desc": _navaid_desc(tags),
                    },
                )
            )

    def way(self, w: osmium.osm.Way) -> None:  # type: ignore[override]
        tags = _tags_to_dict(w.tags)
        coords: list[tuple[float, float]] = []
        for nd in w.nodes:
            if not nd.location.valid():
                # Missing node location — skip the whole way. osmium's
                # flex_mem index normally resolves everything; a missing
                # ref usually means a truncated extract.
                return
            coords.append((nd.location.lon, nd.location.lat))
        if len(coords) < 2:
            return

        natural = tags.get("natural")
        place = tags.get("place")
        seamark = tags.get("seamark:type")

        # Coastline → LineString, emitted verbatim. Per plan/15 the
        # ChartStore treats "crosses coastline" as equivalent to
        # "crosses land" for routing purposes.
        if natural == "coastline":
            line = LineString(coords)
            if not line.intersects(self._bbox_poly):
                return
            self.features.append(
                _make_feature(
                    geom=line,
                    layer="land",
                    props={"osm_id": f"way/{w.id}", "natural": "coastline"},
                )
            )
            return

        # A closed `natural=land` or `place=island` way is a land polygon.
        is_closed = coords[0] == coords[-1] and len(coords) >= 4
        if is_closed and (natural == "land" or place == "island"):
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.intersects(self._bbox_poly):
                return
            self.features.append(
                _make_feature(
                    geom=poly,
                    layer="land",
                    props={
                        "osm_id": f"way/{w.id}",
                        "natural": natural,
                        "place": place,
                    },
                )
            )
            return

        if seamark in _RESTRICTED_TYPES:
            # Most restricted areas are drawn as closed ways. If the
            # upstream data left it open, we close it — a one-node gap
            # is just a digitizing artifact.
            if is_closed:
                poly = Polygon(coords)
            else:
                poly = Polygon(coords + [coords[0]])
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.intersects(self._bbox_poly):
                return
            self.features.append(
                _make_feature(
                    geom=poly,
                    layer="restricted",
                    props={
                        "osm_id": f"way/{w.id}",
                        "seamark:type": seamark,
                        "name": tags.get("seamark:name") or tags.get("name"),
                    },
                )
            )
            return

        # Way-level obstacles are rare but legal (long breakwater
        # obstruction, say). Treat as a LineString on the obstacle
        # layer — the ChartStore's `crosses_obstacle` will handle a
        # mix of point and line geometries.
        if seamark in _OBSTACLE_TYPES:
            line = LineString(coords)
            if not line.intersects(self._bbox_poly):
                return
            self.features.append(
                _make_feature(
                    geom=line,
                    layer="obstacle",
                    props={
                        "osm_id": f"way/{w.id}",
                        "seamark:type": seamark,
                        "name": tags.get("seamark:name") or tags.get("name"),
                        "clearance_m": _parse_clearance(tags, seamark),
                    },
                )
            )


def _tags_to_dict(tags: osmium.osm.TagList) -> dict[str, str]:
    """Shallow-copy an osmium TagList into a plain dict for tag lookups."""
    return {t.k: t.v for t in tags}


def _make_feature(
    *,
    geom: LineString | Point | Polygon,
    layer: str,
    props: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a GeoJSON feature dict with the `bv:layer` property set.

    Drops keys with `None` values so the on-disk GeoJSON stays lean —
    `properties["name"] = null` adds bytes and confuses some readers.
    """
    clean = {k: v for k, v in props.items() if v is not None}
    clean[LAYER_KEY] = layer
    return {
        "type": "Feature",
        "geometry": mapping(geom),
        "properties": clean,
    }


def _seamark_to_sym(tags: dict[str, str]) -> str:
    """Map OSM seamark tags to an OpenCPN sym from `charts_schema.NAVAID_SYMS`.

    Falls back to `"Waypoint"` on any unknown combination; the final
    string is validated against `NAVAID_SYMS` before return.
    """
    stype = tags.get("seamark:type", "")
    sym = "Waypoint"

    if stype == "buoy_lateral":
        colour = (
            tags.get("seamark:buoy_lateral:colour")
            or tags.get("seamark:buoy_lateral:color")
            or tags.get("colour")
            or tags.get("color")
            or ""
        ).lower()
        if "red" in colour:
            sym = "Buoy, Red"
        elif "green" in colour:
            sym = "Buoy, Green"
        else:
            sym = "Buoy, White"
    elif stype == "buoy_cardinal":
        # N/E/S/W cardinals all use black/yellow top-marks. OpenCPN
        # doesn't ship a dedicated cardinal sym, so the convention is
        # the yellow buoy icon.
        category = (tags.get("seamark:buoy_cardinal:category") or "").lower()
        if category in _CARDINAL_QUADRANTS:
            sym = "Buoy, Yellow"
        else:
            sym = "Buoy, Yellow"
    elif stype == "buoy_safe_water":
        sym = "Buoy, White"
    elif stype == "beacon_lateral":
        colour = (
            tags.get("seamark:beacon_lateral:colour")
            or tags.get("seamark:beacon_lateral:color")
            or tags.get("colour")
            or tags.get("color")
            or ""
        ).lower()
        if "red" in colour:
            sym = "Beacon, Red"
        elif "green" in colour:
            sym = "Beacon, Green"
        else:
            sym = "Beacon, White"
    elif stype in ("light_minor", "light_major"):
        sym = "Light"
    elif stype.startswith("buoy_"):
        sym = "Buoy, White"
    elif stype.startswith("beacon_"):
        sym = "Beacon, White"
    elif stype.startswith("light_"):
        sym = "Light"

    if sym not in NAVAID_SYMS:
        return "Waypoint"
    return sym


def _navaid_desc(tags: dict[str, str]) -> str | None:
    """Compose a short human-readable description from light/colour tags.

    Returns `None` if no relevant metadata is present — the GPX emitter
    omits empty `<desc>` fields.
    """
    parts: list[str] = []
    character = tags.get("seamark:light:character")
    if character:
        parts.append(character)
    period = tags.get("seamark:light:period")
    if period:
        parts.append(f"{period}s")
    colour = (
        tags.get("seamark:light:colour")
        or tags.get("seamark:buoy_lateral:colour")
        or tags.get("seamark:beacon_lateral:colour")
        or tags.get("colour")
    )
    if colour:
        parts.append(colour)
    return " ".join(parts) if parts else None


def _parse_clearance(tags: dict[str, str], seamark: str) -> float | None:
    """Extract a depth / clearance value from OSM seamark tags.

    OSM rarely carries clearance directly; wrecks sometimes expose
    `seamark:wreck:depth` in metres. Returns `None` if nothing is
    parseable — consumers treat missing clearance as "unsurveyed".
    """
    candidates = (
        f"seamark:{seamark}:depth",
        "seamark:wreck:depth",
        "seamark:rock:depth",
        "seamark:obstruction:depth",
        "depth",
    )
    for key in candidates:
        raw = tags.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return None


__all__ = [
    "Bbox",
    "OsmExtractMeta",
    "preprocess_osm_extract",
]
