"""NOAA ENC (S-57) preprocessor.

NOAA publishes Electronic Navigational Charts as IHO S-57 Ed 3.1
`.000` files (plus update files `.001`, `.002`, ...). The raw S-57
schema has dozens of feature classes and attributes — far more than
the router needs — and is slow to query directly.

This module preprocesses a single ENC cell into a unified GeoJSON
FeatureCollection using the `bv:layer` tagging scheme from
`charts_schema.py`. The ChartStore loads these GeoJSON caches into
in-memory shapely STRtrees; see plan/15 §Preprocessing.

Usage:

    meta = preprocess_enc_cell(
        Path("data/charts/enc/US4MD01M/US4MD01M.000"),
        Path("data/charts/enc/US4MD01M/US4MD01M.preprocessed.geojson"),
    )
    # meta.cell_id   == "US4MD01M"
    # meta.feature_counts == {"land": 1, "shallow": 37, ...}

S-57 layers consumed (plan/15 §NOAA ENC):

    LNDARE, COALNE          -> land
    DEPARE (DRVAL1 < cutoff) -> shallow
    OBSTRN, WRECKS, UWTROC   -> obstacle (with clearance_m)
    RESARE, MARCUL           -> restricted (navigation prohibited)
    BOYLAT, BOYSAW, BCNLAT,
    LIGHTS, BRIDGE           -> navaid (with OpenCPN sym)

The GDAL S57 driver needs the `OGR_S57_OPTIONS` env var set before
any layer is opened — we do that at import time. `UPDATES=APPLY`
folds `.001`/`.002`/... patch files into the `.000` read;
`SPLIT_MULTIPOINT=ON` makes SOUNDG a point layer; `LIST_AS_STRING=ON`
collapses list-valued attributes (e.g. `COLOUR=3,1`) into a string we
can parse uniformly.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Must be set before GDAL opens any S-57 dataset. Import-time
# `setdefault` keeps us from clobbering an operator override.
os.environ.setdefault(
    "OGR_S57_OPTIONS",
    "UPDATES=APPLY,SPLIT_MULTIPOINT=ON,LIST_AS_STRING=ON",
)

import geopandas as gpd
import pyogrio
from pyogrio.errors import DataLayerError, DataSourceError
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.config import get_settings
from app.logging import get_logger
from app.observability import meter, tracer
from app.services.charts_schema import LAYER_KEY

_log = get_logger(__name__)

_tracer = tracer("app.services.charts_enc")
_m = meter("app.services.charts_enc")
_cells_loaded = _m.create_counter(
    "bv.charts.cells_loaded",
    description="ENC cells preprocessed into GeoJSON caches",
    unit="1",
)


# S-57 COLOUR codes (IHO S-57 Appendix A, attribute COLOUR list).
# We only map the ones that appear on lateral / cardinal / safe-water
# marks. Multi-value lists (e.g. "3,1" for a red/white tower) take the
# first code.
_S57_COLOUR = {
    1: "White",
    2: "Black",
    3: "Red",
    4: "Green",
    5: "Blue",
    6: "Yellow",
    7: "Grey",
    8: "Brown",
    9: "Amber",
    10: "Violet",
    11: "Orange",
    12: "Magenta",
    13: "Pink",
}


# ----------------------------------------------------------------------
# Public dataclass


@dataclass(frozen=True)
class EncCellMeta:
    """Summary of a preprocessed ENC cell.

    Returned by `preprocess_enc_cell`. `feature_counts` is keyed by
    `bv:layer` tag and gives the number of features written for that
    layer; sum equals `len(features)` in the output GeoJSON.
    """

    cell_id: str
    bbox: tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max
    fetched_at: datetime | None
    feature_counts: dict[str, int] = field(default_factory=dict)


# ----------------------------------------------------------------------
# Main entry point


def preprocess_enc_cell(s57_path: Path, out_path: Path) -> EncCellMeta:
    """Read an S-57 cell, emit a `bv:layer`-tagged GeoJSON, return meta.

    `s57_path` points at the `.000` base file (update files `.001`+
    living alongside are folded in by the GDAL S-57 driver because we
    set `UPDATES=APPLY` at import time).

    `out_path` receives one GeoJSON FeatureCollection covering land,
    shallow, obstacle, restricted, and navaid features. Any S-57 layer
    absent from the cell is skipped quietly — plenty of ENC cells
    don't carry all categories.

    Raises `FileNotFoundError` if the `.000` file itself is missing.
    """
    if not s57_path.exists():
        raise FileNotFoundError(f"ENC cell not found: {s57_path}")

    cell_id = s57_path.stem

    with _tracer.start_as_current_span(
        "charts.preprocess",
        attributes={
            "charts.source": "noaa_enc",
            "charts.cell_id": cell_id,
            "charts.path": str(s57_path),
        },
    ) as span:
        settings = get_settings()
        shallow_cutoff = settings.shallow_cutoff_m

        features: list[dict[str, Any]] = []
        counts: dict[str, int] = dict.fromkeys(
            ("land", "shallow", "obstacle", "restricted", "navaid"), 0
        )

        # ---- land -----------------------------------------------------
        land_df = _read_layer(s57_path, "LNDARE")
        if land_df is None or land_df.empty:
            # Fallback: coastline lines. Not polygonizable without extra
            # effort but better than nothing for a cell that only ships
            # COALNE.
            land_df = _read_layer(s57_path, "COALNE")
        if land_df is not None and not land_df.empty:
            land_geom = _safe_union(land_df.geometry)
            if land_geom is not None and not land_geom.is_empty:
                features.append(
                    _feature(
                        land_geom,
                        {LAYER_KEY: "land", "source": "noaa_enc"},
                    )
                )
                counts["land"] = 1

        # ---- shallow --------------------------------------------------
        # Per S-57: DRVAL1 = minimum depth at chart datum (low tide);
        # DRVAL2 = maximum depth. A DEPARE polygon covers a depth range
        # across its area, so only flag polygons where the *entire* area
        # is shallower than the cutoff. A (0, 18.2) polygon spans from
        # drying to 18 m — returning 0 as "available depth" for any
        # point inside it would incorrectly block a deep-water route.
        # Mixed-depth DEPAREs are left to GEBCO's per-point bilinear.
        depare = _read_layer(s57_path, "DEPARE")
        if depare is not None and not depare.empty and "DRVAL2" in depare.columns:
            shallow = depare[depare["DRVAL2"].astype(float) <= shallow_cutoff]
            for _idx, row in shallow.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                drval2 = float(row["DRVAL2"])
                props: dict[str, Any] = {
                    LAYER_KEY: "shallow",
                    "source": "noaa_enc",
                    "drval2_m": drval2,
                }
                if "DRVAL1" in depare.columns and row.get("DRVAL1") is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        props["drval1_m"] = float(row["DRVAL1"])
                features.append(_feature(geom, props))
                counts["shallow"] += 1

        # ---- obstacles -----------------------------------------------
        # Emit one feature per source feature so per-feature attributes
        # (VALSOU / OBJNAM) are preserved.
        for layer_name in ("OBSTRN", "WRECKS", "UWTROC"):
            df = _read_layer(s57_path, layer_name)
            if df is None or df.empty:
                continue
            for _idx, row in df.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                props = {
                    LAYER_KEY: "obstacle",
                    "source": "noaa_enc",
                    "s57_layer": layer_name,
                }
                if "OBJNAM" in df.columns and row.get("OBJNAM"):
                    props["name"] = str(row["OBJNAM"])
                # VALSOU = "value of sounding", meters. Null when the
                # obstruction is unsurveyed — we carry that through as
                # `clearance_m = None` so the router can treat it as
                # "don't cross".
                clearance: float | None = None
                valsou = row.get("VALSOU") if "VALSOU" in df.columns else None
                if valsou is not None:
                    try:
                        f = float(valsou)
                        if f == f:  # not NaN
                            clearance = f
                    except (TypeError, ValueError):
                        clearance = None
                props["clearance_m"] = clearance
                features.append(_feature(geom, props))
                counts["obstacle"] += 1

        # ---- restricted ----------------------------------------------
        # RESARE is the only layer that actually prohibits navigation
        # (security zones, firing ranges). MARCUL (marine cultivation:
        # fish/oyster farms) is a physical obstruction so we fence it
        # off too. CTNARE (caution area) is purely advisory — and in
        # small-scale (US2*/US3*) cells spans entire offshore regions,
        # which would kill routing. DRGARE (dredged area) is a
        # navigable channel — including it blocked the very fairways
        # the boat should be using. Neither belongs in this set.
        restricted_geoms: list[BaseGeometry] = []
        for layer_name in ("RESARE", "MARCUL"):
            df = _read_layer(s57_path, layer_name)
            if df is None or df.empty:
                continue
            for g in df.geometry:
                if g is not None and not g.is_empty:
                    restricted_geoms.append(g)
        if restricted_geoms:
            merged = _safe_union(restricted_geoms)
            if merged is not None and not merged.is_empty:
                features.append(
                    _feature(
                        merged,
                        {LAYER_KEY: "restricted", "source": "noaa_enc"},
                    )
                )
                counts["restricted"] = 1

        # ---- navaids -------------------------------------------------
        for layer_name in ("BOYLAT", "BOYSAW", "BCNLAT", "LIGHTS", "BRIDGE"):
            df = _read_layer(s57_path, layer_name)
            if df is None or df.empty:
                continue
            for _idx, row in df.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                sym = _navaid_sym(layer_name, row)
                name = row.get("OBJNAM") if "OBJNAM" in df.columns else None
                if not name:
                    name = _synth_navaid_name(layer_name, row)
                desc = _navaid_desc(layer_name, row)
                props = {
                    LAYER_KEY: "navaid",
                    "source": "noaa_enc",
                    "s57_layer": layer_name,
                    "sym": sym,
                    "name": str(name) if name is not None else None,
                }
                if desc:
                    props["desc"] = desc
                features.append(_feature(geom, props))
                counts["navaid"] += 1

        total = sum(counts.values())
        span.set_attribute("charts.feature_count_total", total)
        for layer, n in counts.items():
            span.set_attribute(f"charts.feature_count.{layer}", n)

        bbox = _features_bbox(features)
        doc: dict[str, Any] = {
            "type": "FeatureCollection",
            "bbox": list(bbox),
            "bv:cell_id": cell_id,
            "bv:source": "noaa_enc",
            "features": features,
        }

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"), ensure_ascii=False)

        fetched_at = _mtime_as_datetime(s57_path)
        _cells_loaded.add(1, {"source": "noaa_enc"})

        return EncCellMeta(
            cell_id=cell_id,
            bbox=bbox,
            fetched_at=fetched_at,
            feature_counts=counts,
        )


# ----------------------------------------------------------------------
# pyogrio helpers


def _read_layer(path: Path, layer: str) -> gpd.GeoDataFrame | None:
    """Return a GeoDataFrame for `layer`, or `None` if absent / empty.

    ENC cells legitimately ship without many of the layers we read
    (e.g. a harbor approach chart with no BRIDGE features); pyogrio
    surfaces "layer missing" as either a `DataLayerError` /
    `DataSourceError` or an empty dataframe depending on driver
    version. We normalize both shapes to `None`.
    """
    try:
        df = pyogrio.read_dataframe(path, layer=layer)
    except (DataLayerError, DataSourceError, ValueError) as exc:
        _log.debug(
            "enc_layer_missing",
            cell=path.stem,
            layer=layer,
            error=str(exc),
        )
        return None
    except Exception as exc:
        _log.warning(
            "enc_layer_read_failed",
            cell=path.stem,
            layer=layer,
            error=str(exc),
        )
        return None
    if df is None or len(df) == 0:
        return None
    return df


def _safe_union(geoms: Any) -> BaseGeometry | None:
    """`unary_union` that tolerates empty / null inputs."""
    cleaned = [g for g in geoms if g is not None and not g.is_empty]
    if not cleaned:
        return None
    return unary_union(cleaned)


# ----------------------------------------------------------------------
# Navaid category mapping


def _first_colour_code(raw: Any) -> int | None:
    """Parse an S-57 COLOUR attribute into its first integer code.

    With `LIST_AS_STRING=ON` (set at import time), GDAL hands us
    multi-value lists as `"3,1"`; single values come through as ints
    or strings. Any unparseable value returns `None`.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    first = s.split(",")[0].strip()
    try:
        return int(first)
    except ValueError:
        return None


def _navaid_sym(layer: str, row: Any) -> str:
    """Map an S-57 navaid row to an OpenCPN symbol name.

    Kept narrow: the ChartStore only needs enough to render a useful
    sym on the emitted GPX. Unknowns become `"Waypoint"`.
    """
    if layer == "LIGHTS":
        return "Light"
    colour_code = _first_colour_code(
        row.get("COLOUR") if "COLOUR" in getattr(row, "index", []) else None
    )
    colour_name = _S57_COLOUR.get(colour_code) if colour_code is not None else None
    if layer in ("BOYLAT", "BOYSAW"):
        if colour_name in ("Red", "Green", "White", "Yellow"):
            return f"Buoy, {colour_name}"
        if colour_name is None:
            _log.debug("navaid_colour_missing", layer=layer)
        else:
            _log.warning(
                "navaid_colour_unmapped", layer=layer, colour=colour_name
            )
        return "Waypoint"
    if layer == "BCNLAT":
        if colour_name in ("Red", "Green", "White"):
            return f"Beacon, {colour_name}"
        return "Waypoint"
    if layer == "BRIDGE":
        # OpenCPN has no dedicated bridge sym; use a neutral waypoint.
        return "Waypoint"
    return "Waypoint"


def _synth_navaid_name(layer: str, row: Any) -> str:
    """Synthesize a short name when OBJNAM is missing."""
    colour_code = _first_colour_code(
        row.get("COLOUR") if "COLOUR" in getattr(row, "index", []) else None
    )
    colour_name = _S57_COLOUR.get(colour_code) if colour_code is not None else None
    pretty = {
        "BOYLAT": "Lateral Buoy",
        "BOYSAW": "Safe-Water Buoy",
        "BCNLAT": "Lateral Beacon",
        "LIGHTS": "Light",
        "BRIDGE": "Bridge",
    }.get(layer, layer)
    if colour_name:
        return f"{colour_name} {pretty}"
    return pretty


def _navaid_desc(layer: str, row: Any) -> str | None:
    """Build a human-readable desc line from S-57 attrs, or None."""
    parts: list[str] = []
    idx = getattr(row, "index", [])
    if "LITCHR" in idx and row.get("LITCHR") is not None:
        parts.append(f"light={row['LITCHR']}")
    if "SIGGRP" in idx and row.get("SIGGRP") is not None:
        parts.append(f"grp={row['SIGGRP']}")
    if "SIGPER" in idx and row.get("SIGPER") is not None:
        parts.append(f"period={row['SIGPER']}s")
    if "VERCLR" in idx and row.get("VERCLR") is not None:
        parts.append(f"clearance={row['VERCLR']}m")
    if "COLOUR" in idx and row.get("COLOUR") is not None:
        code = _first_colour_code(row.get("COLOUR"))
        if code is not None and code in _S57_COLOUR:
            parts.append(f"colour={_S57_COLOUR[code]}")
    return "; ".join(parts) if parts else None


# ----------------------------------------------------------------------
# Misc helpers


def _feature(geom: BaseGeometry, properties: dict[str, Any]) -> dict[str, Any]:
    """Assemble a GeoJSON Feature dict."""
    return {
        "type": "Feature",
        "geometry": mapping(geom),
        "properties": properties,
    }


def _features_bbox(
    features: list[dict[str, Any]],
) -> tuple[float, float, float, float]:
    """Compute [lon_min, lat_min, lon_max, lat_max] over all features."""
    if not features:
        return (0.0, 0.0, 0.0, 0.0)
    lon_min = lat_min = float("inf")
    lon_max = lat_max = float("-inf")
    for feat in features:
        geom = feat["geometry"]
        for lon, lat in _iter_coords(geom):
            if lon < lon_min:
                lon_min = lon
            if lon > lon_max:
                lon_max = lon
            if lat < lat_min:
                lat_min = lat
            if lat > lat_max:
                lat_max = lat
    if lon_min == float("inf"):
        return (0.0, 0.0, 0.0, 0.0)
    return (lon_min, lat_min, lon_max, lat_max)


def _iter_coords(geom: dict[str, Any]) -> Any:
    """Yield (lon, lat) pairs from any GeoJSON geometry dict."""
    if geom is None:
        return
    t = geom.get("type")
    coords = geom.get("coordinates")
    if t == "Point":
        yield coords[0], coords[1]
    elif t in ("MultiPoint", "LineString"):
        for c in coords:
            yield c[0], c[1]
    elif t in ("MultiLineString", "Polygon"):
        for ring in coords:
            for c in ring:
                yield c[0], c[1]
    elif t == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for c in ring:
                    yield c[0], c[1]
    elif t == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from _iter_coords(g)


def _mtime_as_datetime(path: Path) -> datetime | None:
    """Use the `.000` file's mtime as `fetched_at`.

    Best-effort: ENC cells don't carry a fetch timestamp, but the
    download time lives on the file mtime. Returns None if the stat
    call fails.
    """
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


__all__ = ["EncCellMeta", "preprocess_enc_cell"]
