"""ChartStore — the router's land / obstacle / restricted / depth oracle.

Plan refs: plan/15-charts-bathymetry, plan/03 Part 2. Routing without
real chart data is fiction — either we have ENC ∪ OSM covering the bbox
and a GEBCO tile loaded, or we refuse to plan.

Lifecycle:

1. `ChartStore(base_dir, gebco_path)` is constructed once at app
   startup; its in-memory STRtrees start empty.
2. `await store.ensure_coverage(bbox)` — called from the voyage job's
   `charts_fetching` stage — downloads missing cells / extracts,
   preprocesses them into per-cell GeoJSON (see
   `charts_enc.preprocess_enc_cell` / `charts_osm.preprocess_osm_extract`),
   loads the GEBCO slice, rebuilds per-layer STRtrees. A per-bbox
   `asyncio.Lock` dedupes concurrent voyages in the same area.
3. Sync router methods (`crosses_land`, `crosses_obstacle`,
   `is_restricted`, `available_depth`, `navaids_in`) are pure STRtree
   queries against the in-memory indices.

Coverage policy: if the union of fetched cell / extract bboxes doesn't
cover the request bbox, we raise `ChartsCoverageError` — the planner
maps that to `CHARTS_NOT_AVAILABLE`. No "generic coastline" fallback.

`NullChartStore` stays for unit tests that want to exercise the
router in isolation. Production code must not touch it.
"""  # noqa: RUF002

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from shapely.geometry import (
    LineString,
    Point,
    Polygon,
    box,
    shape,
)
from shapely.strtree import STRtree

from app.config import get_settings
from app.logging import get_logger
from app.observability import meter, tracer
from app.services.charts_fetch import (
    ChartsCoverageError,
    ChartsFetchError,
    EncCellFetchResult,
    OsmExtractFetchResult,
    fetch_enc_cells,
    fetch_osm_extract,
    locate_gebco_tile,
)
from app.services.charts_schema import LAYER_KEY
from app.services.gebco import GebcoBathymetry, load_gebco_bbox

Bbox = tuple[float, float, float, float]  # lat_min, lon_min, lat_max, lon_max
Coord = tuple[float, float]  # lat, lon

log = get_logger(__name__)
_tracer = tracer("app.services.charts")
_m = meter("app.services.charts")
_queries = _m.create_counter(
    "bv.charts.queries",
    description="ChartStore query calls, by kind",
    unit="1",
)
_query_duration = _m.create_histogram(
    "bv.charts.query_duration_seconds",
    description="Wallclock of a ChartStore query (sampled)",
    unit="s",
)
_cells_loaded_gauge = _m.create_counter(
    "bv.charts.cells_loaded",
    description="Cells + extracts loaded into the in-memory ChartStore",
    unit="1",
)


@dataclass(frozen=True)
class ChartCoverage:
    """Returned by `coverage()`. Empty `gaps` means routing is OK."""

    enc_cells: int = 0
    osm_extracts: int = 0
    gebco_tile: str | None = None
    fetched_at: datetime | None = None
    tide_modulated_depth: bool = False
    gaps: list[Bbox] = field(default_factory=list)


@dataclass(frozen=True)
class Waypoint:
    """Subset of GPX `<wpt>` — emitted from `navaids_in`."""

    lat: float
    lon: float
    name: str | None = None
    sym: str | None = None
    desc: str | None = None


class ChartStoreProtocol(Protocol):
    async def coverage(self, bbox: Bbox) -> ChartCoverage: ...
    async def ensure_coverage(self, bbox: Bbox) -> None: ...
    def crosses_land(self, a: Coord, b: Coord) -> bool: ...
    def crosses_obstacle(self, a: Coord, b: Coord) -> bool: ...
    def is_restricted(self, pt: Coord) -> bool: ...
    def chart_depth(self, lat: float, lon: float) -> float | None: ...
    def available_depth(self, lat: float, lon: float, t: datetime) -> float | None: ...
    def navaids_in(self, bbox: Bbox) -> list[Waypoint]: ...


# ---------------------------------------------------------------------------
# Loaded-source bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _LoadedLayers:
    """Per-source decomposition of the preprocessed FeatureCollection."""

    # shapely BaseGeometry subclasses — we don't nail the precise type
    # here because land / restricted mix polygons and linestrings, and
    # the STRtree doesn't care.
    land: list[Any] = field(default_factory=list)
    shallow: list[tuple[Any, float]] = field(default_factory=list)
    obstacles: list[tuple[Any, float | None]] = field(default_factory=list)
    restricted: list[Any] = field(default_factory=list)
    navaids: list[Waypoint] = field(default_factory=list)


@dataclass
class _LoadedSource:
    """An ENC cell or OSM extract loaded into memory."""

    source_id: str
    kind: str  # "enc" | "osm"
    bbox: Bbox
    fetched_at: datetime
    layers: _LoadedLayers = field(default_factory=_LoadedLayers)


# ---------------------------------------------------------------------------
# Real ChartStore
# ---------------------------------------------------------------------------


class ChartStore:
    """Concrete ChartStore backed by NOAA ENC + OSM + GEBCO."""

    def __init__(self, base_dir: Path, gebco_path: Path | None = None) -> None:
        self._base_dir = base_dir
        self._gebco_path = gebco_path
        self._sources: dict[str, _LoadedSource] = {}
        # Rebuilt after each ensure_coverage that adds new sources.
        self._land_tree: STRtree | None = None
        self._land_geoms: list[Any] = []
        self._obstacle_tree: STRtree | None = None
        self._obstacle_geoms: list[Any] = []
        self._obstacle_meta: list[dict[str, Any]] = []
        self._restricted_tree: STRtree | None = None
        self._restricted_geoms: list[Any] = []
        self._shallow_tree: STRtree | None = None
        self._shallow_geoms: list[Any] = []
        self._shallow_drval: list[float] = []
        self._navaid_tree: STRtree | None = None
        self._navaid_points: list[Any] = []
        self._navaid_waypoints: list[Waypoint] = []
        self._gebco: GebcoBathymetry | None = None
        self._gebco_bbox: Bbox | None = None
        self._gebco_tile_id: str | None = None
        self._bbox_locks: dict[tuple[float, ...], asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    # ---- coverage / ensure_coverage ---------------------------------

    async def coverage(self, bbox: Bbox) -> ChartCoverage:
        gaps = self._coverage_gaps(bbox)
        fetched_at = (
            max((s.fetched_at for s in self._sources.values()), default=None)
        )
        return ChartCoverage(
            enc_cells=sum(1 for s in self._sources.values() if s.kind == "enc"),
            osm_extracts=sum(1 for s in self._sources.values() if s.kind == "osm"),
            gebco_tile=self._gebco_tile_id,
            fetched_at=fetched_at,
            tide_modulated_depth=get_settings().tide_modulated_depth,
            gaps=gaps,
        )

    async def ensure_coverage(self, bbox: Bbox) -> None:
        """Download / preprocess / load until `bbox` is covered.

        Raises `ChartsCoverageError` (→ CHARTS_NOT_AVAILABLE upstream)
        if no combination of NOAA ENC + OSM can cover the bbox, or if
        no GEBCO tile is configured.

        Raises `ChartsFetchError` (→ CHARTS_FETCH_FAILED upstream) on
        transient network failures.
        """
        key = self._bbox_key(bbox)
        async with self._global_lock:
            lock = self._bbox_locks.setdefault(key, asyncio.Lock())
        async with lock:
            with _tracer.start_as_current_span(
                "charts.ensure_coverage",
                attributes={"charts.bbox": self._bbox_str(bbox)},
            ) as span:
                # Fast-path: already covered.
                if not self._coverage_gaps(bbox) and self._gebco_covers(bbox):
                    span.set_attribute("charts.cache_hit", True)
                    return
                span.set_attribute("charts.cache_hit", False)

                # GEBCO first — required for every voyage; failure is terminal.
                gebco_path = await locate_gebco_tile(self._gebco_path)
                if self._gebco is None or not self._gebco_covers(bbox):
                    try:
                        self._gebco = load_gebco_bbox(gebco_path, bbox)
                    except ValueError as exc:
                        # bbox outside the loaded netCDF → no bathymetry.
                        # Same policy condition as ENC-plus-OSM gaps:
                        # the planner can't route without depth data, so
                        # raise `CHARTS_NOT_AVAILABLE`.
                        raise ChartsCoverageError(
                            f"GEBCO does not cover bbox {bbox}: {exc}"
                        ) from exc
                    self._gebco_bbox = bbox
                    self._gebco_tile_id = gebco_path.stem

                # Fetch ENC + OSM in parallel. ChartsFetchError bubbles.
                enc_task = asyncio.create_task(fetch_enc_cells(bbox, self._base_dir))
                osm_task = asyncio.create_task(fetch_osm_extract(bbox, self._base_dir))
                enc_results, osm_result = await asyncio.gather(enc_task, osm_task)

                # Preprocess + load in a worker thread — pyogrio / osmium block.
                await asyncio.to_thread(
                    self._preprocess_and_load, enc_results, osm_result, bbox
                )

                # Coverage check. The union of source bboxes must cover
                # the request bbox — otherwise we raise CHARTS_NOT_AVAILABLE.
                gaps = self._coverage_gaps(bbox)
                if gaps:
                    raise ChartsCoverageError(
                        f"bbox not covered by ENC/OSM union; gaps={gaps}"
                    )

                span.set_attribute(
                    "charts.enc_cells",
                    sum(1 for s in self._sources.values() if s.kind == "enc"),
                )
                span.set_attribute(
                    "charts.osm_extracts",
                    sum(1 for s in self._sources.values() if s.kind == "osm"),
                )

    # ---- queries (sync, called from the router hot loop) ------------

    def crosses_land(self, a: Coord, b: Coord) -> bool:
        return self._crosses_layer(a, b, self._land_tree, self._land_geoms, "land")

    def crosses_obstacle(self, a: Coord, b: Coord) -> bool:
        return self._crosses_layer(
            a, b, self._obstacle_tree, self._obstacle_geoms, "obstacle"
        )

    def is_restricted(self, pt: Coord) -> bool:
        t0 = time.monotonic()
        _queries.add(1, {"kind": "restricted"})
        try:
            tree = self._restricted_tree
            if tree is None:
                return False
            p = Point(pt[1], pt[0])  # shapely x=lon, y=lat
            idx = tree.query(p)
            return any(self._restricted_geoms[i].contains(p) for i in idx)
        finally:
            _query_duration.record(
                time.monotonic() - t0, {"kind": "restricted"}
            )

    def chart_depth(self, lat: float, lon: float) -> float | None:
        """ENC DEPARE shallow polygons override; else GEBCO bilinear."""
        _queries.add(1, {"kind": "depth"})
        if self._shallow_tree is not None:
            p = Point(lon, lat)
            idx = self._shallow_tree.query(p)
            for i in idx:
                if self._shallow_geoms[i].contains(p):
                    # DRVAL1 is the *minimum* depth at chart datum.
                    # Treat that as the available depth.
                    return float(self._shallow_drval[i])
        if self._gebco is None:
            return None
        depth = self._gebco.depth(lat, lon)
        return None if depth is None else float(depth)

    def available_depth(self, lat: float, lon: float, t: datetime) -> float | None:
        """`chart_depth` plus optional tide offset (gated)."""
        depth = self.chart_depth(lat, lon)
        if depth is None:
            return None
        if not get_settings().tide_modulated_depth:
            return depth
        # Tide offset is a future plumbing slot. Return chart_depth
        # unchanged until the interpolator lands; see plan/15 §available_depth.
        return depth

    def navaids_in(self, bbox: Bbox) -> list[Waypoint]:
        _queries.add(1, {"kind": "navaid"})
        if self._navaid_tree is None or not self._navaid_waypoints:
            return []
        lat_min, lon_min, lat_max, lon_max = bbox
        region = box(lon_min, lat_min, lon_max, lat_max)
        idx = self._navaid_tree.query(region)
        return [self._navaid_waypoints[i] for i in idx if region.contains(self._navaid_points[i])]

    # ---- internals --------------------------------------------------

    def _crosses_layer(
        self,
        a: Coord,
        b: Coord,
        tree: STRtree | None,
        geoms: list[Any],
        kind: str,
    ) -> bool:
        t0 = time.monotonic()
        _queries.add(1, {"kind": kind})
        try:
            if tree is None:
                return False
            seg = LineString([(a[1], a[0]), (b[1], b[0])])
            idx = tree.query(seg)
            return any(geoms[i].intersects(seg) for i in idx)
        finally:
            _query_duration.record(
                time.monotonic() - t0, {"kind": kind}
            )

    # ---- loading ----------------------------------------------------

    def _preprocess_and_load(
        self,
        enc_results: list[EncCellFetchResult],
        osm_result: OsmExtractFetchResult | None,
        request_bbox: Bbox,
    ) -> None:
        """Run preprocessors where needed, load results into memory."""
        # Lazy imports — the preprocessors pull pyogrio / osmium, which
        # we don't want on the critical path for routes that already
        # have cached preprocessed GeoJSON.
        from app.services.charts_enc import preprocess_enc_cell
        from app.services.charts_osm import preprocess_osm_extract

        for enc in enc_results:
            if enc.cell_id in self._sources:
                continue
            out_path = enc.s57_path.with_suffix(".preprocessed.geojson")
            if not out_path.exists():
                preprocess_enc_cell(enc.s57_path, out_path)
            bbox = _bbox_from_geojson(out_path)
            self._sources[enc.cell_id] = self._load_source(
                source_id=enc.cell_id,
                kind="enc",
                bbox=bbox,
                fetched_at=enc.fetched_at,
                geojson_path=out_path,
            )
            _cells_loaded_gauge.add(1, {"source": "noaa_enc"})

        if osm_result is not None and osm_result.extract_id not in self._sources:
            out_path = osm_result.pbf_path.with_suffix(".preprocessed.geojson")
            if not out_path.exists():
                preprocess_osm_extract(
                    osm_result.pbf_path, request_bbox, out_path
                )
            bbox = _bbox_from_geojson(out_path)
            self._sources[osm_result.extract_id] = self._load_source(
                source_id=osm_result.extract_id,
                kind="osm",
                bbox=bbox,
                fetched_at=osm_result.fetched_at,
                geojson_path=out_path,
            )
            _cells_loaded_gauge.add(1, {"source": "osm"})

        self._rebuild_indices()

    def _load_source(
        self,
        *,
        source_id: str,
        kind: str,
        bbox: Bbox,
        fetched_at: datetime,
        geojson_path: Path,
    ) -> _LoadedSource:
        src = _LoadedSource(
            source_id=source_id, kind=kind, bbox=bbox, fetched_at=fetched_at
        )
        with _tracer.start_as_current_span(
            "charts.load",
            attributes={
                "charts.source": f"{kind}",
                "charts.source_id": source_id,
            },
        ) as span:
            with geojson_path.open("r", encoding="utf-8") as f:
                fc = json.load(f)
            feats = fc.get("features", [])
            for feat in feats:
                layer = (feat.get("properties") or {}).get(LAYER_KEY)
                geom_json = feat.get("geometry")
                if geom_json is None:
                    continue
                try:
                    geom = shape(geom_json)
                except Exception:  # pragma: no cover
                    continue
                props = feat.get("properties") or {}
                if layer == "land":
                    src.layers.land.append(geom)
                elif layer == "shallow":
                    drval = float(props.get("drval1_m") or 0.0)
                    src.layers.shallow.append((geom, drval))
                elif layer == "obstacle":
                    src.layers.obstacles.append(
                        (geom, props.get("clearance_m"))
                    )
                elif layer == "restricted":
                    src.layers.restricted.append(geom)
                elif layer == "navaid":
                    # navaid geometry is a Point; props carry sym / name / desc.
                    if not isinstance(geom, Point):
                        continue
                    src.layers.navaids.append(
                        Waypoint(
                            lat=geom.y,
                            lon=geom.x,
                            name=props.get("name"),
                            sym=props.get("sym"),
                            desc=props.get("desc"),
                        )
                    )
            span.set_attribute("charts.feature_count_total", len(feats))
        return src

    def _rebuild_indices(self) -> None:
        land_geoms: list[Any] = []
        obstacle_geoms: list[Any] = []
        obstacle_meta: list[dict[str, Any]] = []
        restricted_geoms: list[Any] = []
        shallow_geoms: list[Any] = []
        shallow_drval: list[float] = []
        navaid_points: list[Any] = []
        navaid_waypoints: list[Waypoint] = []

        for src in self._sources.values():
            land_geoms.extend(src.layers.land)
            for g, clearance in src.layers.obstacles:
                obstacle_geoms.append(g)
                obstacle_meta.append({"clearance_m": clearance})
            restricted_geoms.extend(src.layers.restricted)
            for g, drval in src.layers.shallow:
                shallow_geoms.append(g)
                shallow_drval.append(drval)
            for wp in src.layers.navaids:
                navaid_waypoints.append(wp)
                navaid_points.append(Point(wp.lon, wp.lat))

        self._land_geoms = land_geoms
        self._land_tree = STRtree(land_geoms) if land_geoms else None
        self._obstacle_geoms = obstacle_geoms
        self._obstacle_meta = obstacle_meta
        self._obstacle_tree = STRtree(obstacle_geoms) if obstacle_geoms else None
        self._restricted_geoms = restricted_geoms
        self._restricted_tree = STRtree(restricted_geoms) if restricted_geoms else None
        self._shallow_geoms = shallow_geoms
        self._shallow_drval = shallow_drval
        self._shallow_tree = STRtree(shallow_geoms) if shallow_geoms else None
        self._navaid_points = navaid_points
        self._navaid_waypoints = navaid_waypoints
        self._navaid_tree = STRtree(navaid_points) if navaid_points else None

        log.info(
            "charts.indices_rebuilt",
            sources=len(self._sources),
            land=len(land_geoms),
            obstacles=len(obstacle_geoms),
            restricted=len(restricted_geoms),
            shallow=len(shallow_geoms),
            navaids=len(navaid_waypoints),
        )

    # ---- coverage helpers -------------------------------------------

    def _coverage_gaps(self, request: Bbox) -> list[Bbox]:
        """Return the `request` bbox minus the union of loaded source bboxes.

        Result is either empty (full coverage) or a list of residual
        bboxes describing the uncovered envelope. For the MVP we only
        report the axis-aligned envelope of the residual multipolygon —
        the planner treats any non-empty list as "no coverage."
        """
        if not self._sources:
            return [request]
        request_poly = _bbox_to_polygon(request)
        union = None
        for src in self._sources.values():
            src_poly = _bbox_to_polygon(src.bbox)
            union = src_poly if union is None else union.union(src_poly)
        if union is None:
            return [request]
        residual = request_poly.difference(union)
        if residual.is_empty:
            return []
        minx, miny, maxx, maxy = residual.bounds
        # shapely gives (minx, miny, maxx, maxy) = (lon, lat, lon, lat).
        return [(miny, minx, maxy, maxx)]

    def _gebco_covers(self, bbox: Bbox) -> bool:
        return self._gebco is not None and self._gebco.covers(bbox)

    @staticmethod
    def _bbox_key(bbox: Bbox) -> tuple[float, ...]:
        return tuple(round(v, 3) for v in bbox)

    @staticmethod
    def _bbox_str(bbox: Bbox) -> str:
        lat_min, lon_min, lat_max, lon_max = bbox
        return f"{lat_min:.4f},{lon_min:.4f},{lat_max:.4f},{lon_max:.4f}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bbox_to_polygon(bbox: Bbox) -> Polygon:
    lat_min, lon_min, lat_max, lon_max = bbox
    return box(lon_min, lat_min, lon_max, lat_max)


def _bbox_from_geojson(path: Path) -> Bbox:
    with path.open("r", encoding="utf-8") as f:
        fc = json.load(f)
    b = fc.get("bbox")
    if b and len(b) == 4:
        lon_min, lat_min, lon_max, lat_max = b
        return (lat_min, lon_min, lat_max, lon_max)
    # Fall back to scanning features.
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")
    for feat in fc.get("features", []):
        geom = feat.get("geometry")
        if geom is None:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        gxmin, gymin, gxmax, gymax = g.bounds
        min_lon = min(min_lon, gxmin)
        min_lat = min(min_lat, gymin)
        max_lon = max(max_lon, gxmax)
        max_lat = max(max_lat, gymax)
    if min_lat == float("inf"):
        return (0.0, 0.0, 0.0, 0.0)
    return (min_lat, min_lon, max_lat, max_lon)


# ---------------------------------------------------------------------------
# Null stub for unit tests
# ---------------------------------------------------------------------------


class NullChartStore:
    """Development stub — treats the entire planet as navigable water.

    DO NOT use in production. Real voyage planning REQUIRES the real
    ChartStore per plan/15; the router refuses to plan without chart
    coverage. This class exists so the router + planner can be unit-
    tested in isolation from chart ingest.
    """

    async def coverage(self, bbox: Bbox) -> ChartCoverage:
        return ChartCoverage(
            enc_cells=0, osm_extracts=0, gebco_tile=None,
            fetched_at=None, tide_modulated_depth=False, gaps=[],
        )

    async def ensure_coverage(self, bbox: Bbox) -> None:
        return None

    def crosses_land(self, a: Coord, b: Coord) -> bool:
        return False

    def crosses_obstacle(self, a: Coord, b: Coord) -> bool:
        return False

    def is_restricted(self, pt: Coord) -> bool:
        return False

    def chart_depth(self, lat: float, lon: float) -> float | None:
        return 100.0

    def available_depth(self, lat: float, lon: float, t: datetime) -> float | None:
        return 100.0

    def navaids_in(self, bbox: Bbox) -> list[Waypoint]:
        return []


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_instance: ChartStore | NullChartStore | None = None


def get_chart_store() -> ChartStore | NullChartStore:
    """Return the process-wide ChartStore.

    In prod (`BV_CHART_STORE_MODE=real`) this is a real `ChartStore`
    rooted at `BV_CHARTS_DIR` with GEBCO at `BV_GEBCO_PATH`. In dev /
    tests (`BV_CHART_STORE_MODE=null`) it's the `NullChartStore` stub
    so the router + planner can be exercised without chart ingest.

    A single instance is shared across voyage jobs — its in-memory
    STRtrees accumulate loaded cells, and the per-bbox `asyncio.Lock`
    inside `ensure_coverage` dedupes concurrent voyages in the same
    area (plan/15 §Chart fetching as a background job stage).
    """
    global _instance
    if _instance is not None:
        return _instance
    settings = get_settings()
    if settings.chart_store_mode == "null":
        log.info("charts.store.null_mode")
        _instance = NullChartStore()
    else:
        log.info(
            "charts.store.real_mode",
            charts_dir=str(settings.charts_dir),
            gebco_path=str(settings.gebco_path) if settings.gebco_path else None,
        )
        _instance = ChartStore(
            base_dir=settings.charts_dir,
            gebco_path=settings.gebco_path,
        )
    return _instance


def reset_chart_store() -> None:
    """Drop the cached store. Test hook; also useful for a future SIGHUP."""
    global _instance
    _instance = None


# Re-exports for ergonomics (existing imports like
# `from app.services.charts import ChartsCoverageError` keep working).
__all__ = [
    "Bbox",
    "ChartCoverage",
    "ChartStore",
    "ChartStoreProtocol",
    "ChartsCoverageError",
    "ChartsFetchError",
    "NullChartStore",
    "Waypoint",
    "get_chart_store",
    "reset_chart_store",
]
