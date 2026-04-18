# 02 — Architecture

**Status:** draft

## Goals

- Thin HTTP layer; business logic is pure Python that can run offline.
- External I/O (NOAA, Open-Meteo, disk) is isolated behind client
  classes with clear cache boundaries.
- Weather routing (doc 04) is the core of the product. The isochrone
  kernel is a pure-numpy module with no I/O, driven by a cached
  forecast field and an obstacle index.
- Scoring (doc 05) is a pure post-hoc summary, deterministic and
  testable without network.
- Easy to snapshot a replay (request → cached data → candidates) so we
  can regression-test planning changes against real forecasts.
- **Observable from day one.** Logs, traces, and metrics are plumbed
  through before any real business logic lands (see doc 14).

## Layers

```
┌─────────────────────────────────────────────────────┐
│ app/routers        FastAPI endpoints (HTTP shell)   │
├─────────────────────────────────────────────────────┤
│ app/schemas        Pydantic request/response        │
├─────────────────────────────────────────────────────┤
│ app/services       Planning, routing, scoring,      │
│                    contingencies, NL summary, GPX   │
├─────────────────────────────────────────────────────┤
│ app/clients        Open-Meteo, NOAA T&C + station   │
│                    discovery. Retry + cache wrapping│
├─────────────────────────────────────────────────────┤
│ app/models         SQLAlchemy ORM (cache + voyages) │
│ app/db.py          engine, session, Base            │
├─────────────────────────────────────────────────────┤
│ app/observability  OTel bootstrap, tracer / meter   │
│ app/logging        structlog + trace-id injection   │
└─────────────────────────────────────────────────────┘
```

**Rule:** `services` may import `clients` and `models`; `clients` and
`models` may not import `services` or `routers`. `observability` and
`logging` are imported by everything but import nothing from the rest
of the app. Keeps the blast radius predictable.

## Package layout

```
app/
  __init__.py
  main.py           FastAPI app + lifespan
  config.py         Settings (pydantic-settings)
  db.py             engine, async sessionmaker, Base
  logging.py        structlog config + OTel trace-id injection
  observability.py  OpenTelemetry setup (tracer, meter, exporters)
  data/
    polars/         Nominal ORC-derived polars (*.pol CSV)
    pois.gpx        Seeded POIs; supplemented via BV_POI_DIRS
  clients/
    __init__.py
    open_meteo.py      Marine forecast client (wind, waves, currents)
    noaa.py            Tides & currents + station metadata
    cache.py           Cache-or-fetch wrapper (TTL + SQLite backing)
  models/
    __init__.py
    voyage.py          SQLAlchemy: Voyage (with gpx_blob), CandidatesIndex
    boat_profile.py    SQLAlchemy: BoatProfile
    forecast.py        Cached forecast / tide / station blobs
  schemas/
    __init__.py
    gpx.py             Pydantic mirrors of <gpx>, <metadata>, <wpt>, <rte>
    extensions.py      Typed bv: extension model
    request.py         VoyageRequest, BoatProfile, TimeWindow
  services/
    __init__.py
    jobs.py            JobRegistry: async task lifecycle, scheduler,
                       crash recovery (see doc 15)
    planner.py         Runs one voyage job: stage progression,
                       progress, error-code mapping; emits PlanTrace
    router.py          Isochrone weather-routing kernel (numpy)
    forecast_field.py  Cached field + spatial/temporal interpolation
    charts.py          ChartStore: land / obstacles / restricted /
                       bathymetry / navaids (see doc 03)
    polars.py          Polar file load + bilinear (TWA, TWS) → BSP
    scorer.py          Pure scoring (post-hoc; see doc 05)
    contingency.py     Tap-out annotations + escape-hatch re-routes
    summary.py         NL pros/cons generation
    gpx.py             Serializer: our schemas ↔ gpxpy tree
  routers/
    __init__.py
    health.py
    voyages.py         POST /voyages, GET /voyages/{id},
                       GET .../gpx, GET .../trace
tests/
  unit/             Pure-logic tests (no network, no disk)
  integration/      App-level tests with local SQLite and replayed HTTP
  fixtures/         JSON replays of upstream API responses + golden routes
plan/               This directory
```

## Heavy-lift dependencies (land at M2)

- `numpy>=2.0` — vector math for the isochrone kernel and grid interp
- `scipy>=1.14` — bilinear grid interpolation
- `pyproj>=3.7` — geodesic step + bearing (WGS84)
- `shapely>=2.0` — STRtree spatial indices
- `netCDF4>=1.7` + `xarray>=2024.10` — GEBCO bathymetry (doc 03)
- `pyogrio>=0.9` — NOAA ENC / vector chart ingest (doc 03; lands
  alongside the M4 NOAA ENC upgrade, but the `ChartStore` interface
  and `shapely` indices land at M2)

Not added at M0 to keep the initial image small; introduced alongside
`services/router.py` in M2 (doc 13).

## Async model

- FastAPI handlers are `async`.
- `clients/*` use `httpx.AsyncClient` with `tenacity` retries.
- SQLAlchemy uses `asyncio` engine + `aiosqlite`.
- **Pure services** (`router`, `scorer`, `polars`, `obstacles`,
  `summary`, `gpx`) are sync numpy / pure-Python. Trivial to test and
  reason about; no I/O.
- `planner` is `async` — fans out forecast prefetch and per-candidate
  routing.
- The isochrone kernel (`router.py`) is CPU-bound numpy. `planner`
  dispatches it via `asyncio.to_thread` (or a shared thread pool) so
  it can run concurrently without blocking the event loop.

## Concurrency

- Forecast prefetch runs concurrent upstream requests (`asyncio.gather`)
  under a bounded semaphore (`BV_MAX_CONCURRENT_FETCHES`, default 4).
- Per-candidate isochrone runs are fanned out under a separate
  semaphore (`BV_MAX_CONCURRENT_ROUTES`, default = CPU count). Each
  candidate is independent — no shared mutable state.

## Configuration

Single `Settings` object from `pydantic-settings`. Env vars prefixed
`BV_`. Canonical settings live in `app/config.py`; no scattered
`os.environ` reads.

## Error model

- Domain errors → `HTTPException` (400/404/409/422).
- Router failures (`ROUTE_BLOCKED`, `ROUTE_TIMEOUT`, `ROUTE_NO_COVERAGE`,
  `ROUTE_LIMIT_EXCEEDED`) → per-candidate, surfaced in `skipped`. Whole
  voyage returns 409/503 only if *no* candidates succeed.
- Upstream failures → retry + fall back to cache; if both fail, 503
  with a structured reason.
- Unexpected errors → 500 with a correlation id in the response body
  and log.

## Observability

First-class concern — see doc 14 for the full design. Summary:

- OpenTelemetry SDK initialized in
  `app.observability.setup_observability()` called from the FastAPI
  lifespan.
- Auto-instrumentation for FastAPI, httpx, and SQLAlchemy gives us
  request, upstream, and DB spans for free.
- Custom spans mark each planning phase (prefetch,
  `router.plan_candidate` per candidate, score, contingency, emit).
- Structured logs via `structlog`; each event carries `trace_id`,
  `span_id`, `correlation_id`, and (once known) `voyage_id`.
- Metrics catalog: upstream latency, cache hit rate, planning
  duration, candidates produced/rejected per reason, scoring component
  distribution, router wallclock and step histograms.
- Exporter defaults to `console`; `compose.yaml`'s `observability`
  profile brings up `grafana/otel-lgtm` for a local Grafana backend.

## Decisions

- Plans run as **async background jobs** (doc 15), not
  request/response. `POST /voyages` returns `202` with a voyage id;
  clients poll `GET /voyages/{id}` or subscribe to `/events`.
  In-process asyncio tasks + SQLite-backed state — no Redis in MVP.
- Alembic migrations from day one.
- Weather routing is core, not a stretch; isochrone kernel lands at M2.
- Chart data (NOAA ENC, OpenSeaMap, GEBCO) is **required**, not
  optional. Voyages fail with `CHARTS_NOT_AVAILABLE` rather than fall
  back to synthetic coastlines (doc 03).
