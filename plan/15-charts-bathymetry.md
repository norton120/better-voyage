# 15 — Charts, bathymetry, and navaids

**Status:** draft

Weather routing without chart data is fiction. The router needs real
land polygons, bathymetry, obstacles, and restricted areas. This doc
owns the **what / where / how** of chart ingest.

**Policy:** the router either has real chart data for the voyage bbox
or it **refuses to plan**. No degraded fallback, no synthetic coastline
stand-ins — routing on bogus chart data silently invites grounding.
Chart fetching is handled inside the voyage's async job (doc 16), so
slow first-run ingest doesn't block the HTTP request.

## What the router needs

Per propagated motion `(a → b, t)` in the isochrone inner loop:

| Check                     | Layer                                    |
| ------------------------- | ---------------------------------------- |
| Land crossing             | Coastline / land polygons                |
| Obstacle crossing         | Wrecks, rocks, obstructions              |
| Restricted / caution area | RESARE, CTNARE, MARCUL / OSM restricted  |
| Water deep enough         | Chart depth + boat draft + optional tide |

For emitted GPX (user awareness, not routing):

| Concern                         | Layer                        |
| ------------------------------- | ---------------------------- |
| Navaids on route                | Buoys, beacons, lights       |
| Bridge clearance (masted boats) | BRIDGE `VERCLR` — post-MVP   |
| TSS awareness                   | Penalize crossing — post-MVP |

## Data sources (all three are required)

### NOAA ENC — US waters

Primary in US waters. Free, updated weekly, IHO S-57 Ed 3.1.

- Downloads: <https://www.charts.noaa.gov/ENCs/ENCs.shtml>
- Pre-converted (GeoPackage / Shapefile / GeoJSON):
  <https://encdirect.noaa.gov/>
- Reader: GDAL S57 driver via `pyogrio`.

Layers consumed: `LNDARE`, `COALNE`, `DEPARE`, `DEPCNT`, `DRGARE`,
`OBSTRN`, `WRECKS`, `UWTROC`, `RESARE`, `CTNARE`, `MARCUL`, `BOYLAT`,
`BOYSAW`, `BCNLAT`, `LIGHTS`, `BRIDGE`.

### OpenSeaMap — non-US waters (and supplement for US)

OSM-derived marine data, global coverage.

- Source: <https://www.openseamap.org/>
- Format: OSM XML / PBF or Overpass API.
- Reader: `pyosmium` for bulk PBF; `httpx` for Overpass fragments.

Features lifted: `natural=coastline` (land boundaries); `seamark:type`
values for buoys / beacons / lights / wrecks / rocks / restricted
areas / marine farms.

### GEBCO — global bathymetry

15-arc-second (~450 m) global grid. Free, netCDF.

- Source: <https://www.gebco.net/data_and_products/gridded_bathymetry_data/>
- Reader: `xarray` + `netCDF4`.

Bathymetry everywhere. Where NOAA `DEPARE` is available, it overrides
GEBCO at that point (harbor ENC soundings are orders of magnitude
finer than GEBCO).

## Bbox coverage policy

Before routing, `ChartStore.coverage(bbox)` must return `gaps=[]`
across all three concerns:

- **Land / obstacles / restricted:** bbox fully covered by ENC
  ∪ OSM. US bbox → ENC must cover; non-US bbox → OSM must cover;
  mixed bbox → the two partitions combine.
- **Bathymetry:** GEBCO tile must be loaded for the bbox. ENC DEPARE
  upgrades where available but is not required.

If any gap remains, the voyage job terminates with
`CHARTS_NOT_AVAILABLE` (doc 10). There is **no** Natural Earth
fallback, no "generic coastline" fill — we either know the water or
we don't plan.

## Chart fetching is a background job

Fetching ENC + OSM + GEBCO takes seconds to many minutes. It runs
inside the voyage's async job (doc 16) in the `charts_fetching` →
`charts_preprocessing` stages:

```
POST /voyages → 202
  ↓  (scheduler picks up the queued row)
status = charts_fetching
  ↓  per-bbox asyncio.Lock in ChartStore.ensure_coverage —
     concurrent voyages in the same area deduplicate
download ENC cells + OSM extract + GEBCO tile
(progress: cells_done / cells_total, published every 2 s)
  ↓
status = charts_preprocessing
  ↓
build unified layers (land / obstacles / restricted / navaids /
shallow) per cell, cached as GeoJSON
  ↓
status = forecast_prefetching → ...
```

The `python -m app.charts fetch --region chesapeake` CLI runs the same
machinery **synchronously** for operators who want to pre-seed a
region. Recommended before leaving the dock for areas with no
connectivity.

## Storage layout

Cache dir: `BV_CHARTS_DIR` (default `/data/charts`).

```
/data/charts/
  enc/
    US4MD01M/
      US4MD01M.000
      US4MD01M.preprocessed.geojson
  osm/
    chesapeake.osm.pbf
    chesapeake.preprocessed.geojson
  gebco/
    gebco_2024_sub_ice_topo.nc
  index.sqlite          # cell + extract coverage, fetch metadata,
                        # staleness, per-bbox fetch locks
```

## Preprocessing

Raw S-57 and OSM are slow to query. On first load we preprocess each
ENC cell / OSM extract into a GeoJSON cache:

- `land` — unified multipolygon.
- `shallow` — DEPARE polygons with `DRVAL1 < BV_SHALLOW_CUTOFF_M`
  (default 2 m). OSM doesn't contribute here; GEBCO covers bathymetry
  outside ENC.
- `obstacles` — OBSTRN + WRECKS + UWTROC (ENC) unioned with
  `seamark:type=wreck` / `rock` / `obstruction` (OSM) and
  user-declared hazards from `pois.gpx`, each carrying a
  `clearance_m` attr (null if unsurveyed).
- `restricted` — RESARE + CTNARE + MARCUL (ENC) + OSM
  `seamark:type=restricted_area`.
- `navaids` — points with `sym` pre-mapped from S-57 category codes
  and `seamark:*` tags to OpenCPN symbol names.

The router queries preprocessed layers only.

## `ChartStore`

```python
class ChartStore:
    async def coverage(self, bbox: Bbox) -> ChartCoverage: ...
    async def ensure_coverage(
        self, bbox: Bbox, *, progress: ProgressCb | None = None,
    ) -> None: ...
    def crosses_land(self, a: Coord, b: Coord) -> bool: ...
    def crosses_obstacle(self, a: Coord, b: Coord) -> bool: ...
    def is_restricted(self, pt: Coord) -> bool: ...
    def chart_depth(self, lat: float, lon: float) -> float | None: ...
    def available_depth(
        self, lat: float, lon: float, t: datetime
    ) -> float | None: ...
    def navaids_in(self, bbox: Bbox) -> list[Waypoint]: ...
```

`ensure_coverage` is the job-side entry point. It:

1. Acquires a per-bbox `asyncio.Lock` to dedupe concurrent fetches.
2. Downloads missing ENC cells / OSM extracts / GEBCO tile.
3. Preprocesses into GeoJSON caches.
4. Loads unified layers into in-memory shapely STRtrees.
5. Calls the `progress` callback throughout.

`available_depth(lat, lon, t)` = `chart_depth + tide_offset(lat, lon, t)`.
Tide offset interpolates distance-weighted from nearby NOAA tide
stations (`BV_TIDE_INTERPOLATION_RADIUS_NM`, default 25 nm). No station
in range → offset = 0. Gated by `BV_TIDE_MODULATED_DEPTH`
(default `false` at M2; flipped once the interpolator is validated).

## Router integration

Doc 04's propagation step consults `ChartStore` four times per motion:

```python
if charts.crosses_land(pt, new):            continue
if charts.crosses_obstacle(pt, new):        continue
if charts.is_restricted(new):               continue
depth = charts.available_depth(new.lat, new.lon, t)
if depth is None:                           continue
if depth < boat.draft_m + boat.min_depth_m: continue
```

`pois.gpx` hazards are unioned into `charts.obstacles` during
preprocessing, so user-declared hazards behave identically to
ENC-/OSM-derived ones.

## Navaids in emitted GPX

When emitting a voyage (doc 09), navaids within `NAVAID_BBOX_PAD_NM`
(default 2 nm) of any route leg are included as top-level `<wpt>`
with `sym` (pre-mapped), short `name` (e.g. `R '4'`), and `desc`
(color / shape / light characteristic).

User-facing context, not a router input. OpenCPN renders these
alongside the route.

## Coverage block in voyage response

`voyage.bv:coverage.charts`:

```json
{
  "enc_cells":            12,
  "osm_extracts":         0,
  "gebco_tile":           "gebco_2024_sub_ice_topo",
  "fetched_at":           "2026-04-17T12:00:00Z",
  "tide_modulated_depth": false
}
```

No `primary` / `fallback` field — the policy is "all three sources
contribute, or we fail." There is no reduced-coverage success mode.

## Failure modes (terminal for the voyage job)

| Code                   | Stage           | When                                                                                    |
| ---------------------- | --------------- | --------------------------------------------------------------------------------------- |
| `CHARTS_NOT_AVAILABLE` | charts_fetching | Bbox has gaps ENC ∪ OSM can't cover, or GEBCO tile unavailable.                         |
| `CHARTS_FETCH_FAILED`  | charts_fetching | Network / upstream error during the fetch. Retryable via resubmit.                      |
| `CHARTS_STALE`         | charts_fetching | Cached cells older than `BV_CHARTS_MAX_AGE_DAYS` (default 90) and refresh fetch failed. |

All surface as `voyage.error.code` with `status=failed` (doc 10).

## First-run UX

1. `docker compose up` — app starts with empty `/data/charts`.
2. First `POST /voyages` returns `202` immediately; the job moves to
   `charts_fetching`.
3. Client polls `GET /voyages/{id}` and sees `progress.stage =
   "charts_fetching"` with byte / cell counts. Typical first-run
   latency 30 s – few minutes per ~1°×1° region, depending on link.
4. Subsequent voyages in the same area: cache hit,
   `charts_fetching` stage completes in milliseconds.
5. Long passages in a fresh area where the fetch fails:
   `status=failed`, `error.code=CHARTS_NOT_AVAILABLE` or
   `CHARTS_FETCH_FAILED` — client retries or pre-seeds offline via
   the CLI.

## Dependencies (land at M2)

- `pyogrio>=0.9` — S-57 / Shapefile / GeoJSON I/O (wraps GDAL)
- `pyosmium>=4.0` — OSM PBF / XML reader
- `shapely>=2.0` — STRtree (shared with router)
- `netCDF4>=1.7` + `xarray>=2024.10` — GEBCO
- `geopandas>=1.0` — convenient during preprocessing

`Dockerfile` must install `gdal-bin libgdal-dev libspatialite-dev`
(native dep of `pyogrio` / `pyosmium`).

## Observability

Spans (children of `job.charts_fetching` / `job.charts_preprocessing`):

- `charts.ensure_coverage` — bbox, `cells_missing`,
  `wallclock_seconds`
- `charts.fetch` — `source` (`noaa_enc` / `osm` / `gebco`),
  `bytes_downloaded`, `wallclock_seconds`
- `charts.load` — per cell / extract
- `charts.preprocess` — per cell / extract, first-load only

Metrics:

- `bv.charts.queries{kind=land|obstacle|restricted|depth|navaid}` counter
- `bv.charts.cells_loaded` gauge
- `bv.charts.fetch_bytes{source}` counter
- `bv.charts.fetch_seconds{source}` histogram
- `bv.charts.query_duration_seconds{kind}` histogram (sampled —
  don't instrument every hot-path call)

## Notes

- **CM93** — commercial worldwide S-57 superset many cruisers have
  licensed. Read user-supplied files; don't redistribute.
- **ENC refresh cadence** — Monthly pull for MVP.
