# 13 — Roadmap

**Status:** draft

Milestones are ordered. Don't skip ahead — each builds on the last.

## M0 — Repo skeleton + observability foundation ✅

*Goal: Docker up, `/health` returns 200, pytest green, OTel wired so
the next milestone starts with traces/logs/metrics from the jump.*

- [x] Docker / compose / pyproject / uv

- [x] FastAPI app, `/health`

- [x] Pytest skeleton with one smoke test

- [x] OpenTelemetry SDK + auto-instrumentation (FastAPI, httpx,
  
      SQLAlchemy, logging)

- [x] structlog with `trace_id` / `span_id` injection

- [x] `compose.yaml` `observability` profile (`grafana/otel-lgtm`)

- [x] Settings for exporter mode / endpoint / sample ratio

## M1 — Data sources ✅

*Goal: talk to both upstreams, cache to SQLite, work offline after the
first fetch. Every upstream call emits a span and a cache-hit metric.*

- [x] `clients/open_meteo.py` — marine forecast incl. ocean currents
- [x] `clients/noaa.py` — tide predictions + station metadata
- [x] Cache wrapper (TTL + SQLite), emits `bv.cache.lookups`
- [x] Smoke: fetch Annapolis tide for tomorrow, re-run offline
- [x] Replay fixtures under `tests/fixtures/http/`

## M2 — Async jobs + charts + isochrone router + one candidate

*Goal: end-to-end plan pipeline. `POST /voyages` returns `202`, job
runs in the background: fetches real charts (ENC + OSM + GEBCO),
prefetches forecast, runs isochrone for one departure, scores,
finalizes. Fails hard with `CHARTS_NOT_AVAILABLE` if any of the three
chart sources can't cover the bbox.*

**Async job infrastructure** (see doc 15) ✅

- [x] `services/jobs.py` — `JobRegistry`, async task lifecycle,
  
      scheduler coroutine, crash-recovery sweep on startup

- [x] `voyages` table status / progress / error columns (doc 11)

- [x] `POST /voyages` → `202` with voyage id

- [x] `GET /voyages/{id}` returns status + progress + voyage (when done)

- [x] `POST /voyages/{id}/cancel`

- [x] Idempotency via `inputs_hash`

- [x] Jobs spans + metrics (`bv.jobs.*`)

**Chart ingest** (see doc 03, Part 2)

- [x] Heavy-lift deps: `numpy`, `scipy`, `pyproj`, `shapely`,
      `pyogrio`, `osmium` (the former `pyosmium` 4.x), `netCDF4`,
      `xarray`, `geopandas`

- [x] `Dockerfile` installs `gdal-bin libgdal-dev libspatialite-dev`

- [x] `services/charts.py` — `ChartStore` with `ensure_coverage`,
      per-bbox `asyncio.Lock`, fetch→preprocess→STRtree pipeline.
      `NullChartStore` stays for unit tests; production flips to real
      via `BV_CHART_STORE_MODE=real`. `CHARTS_NOT_AVAILABLE` /
      `CHARTS_FETCH_FAILED` surface through `PlannerError`
      (`app/services/charts.py`).

- [x] NOAA ENC reader (S-57 via `pyogrio`) → preprocessed GeoJSON
      (`app/services/charts_enc.py` — reads LNDARE / COALNE / DEPARE /
      OBSTRN / WRECKS / UWTROC / RESARE / CTNARE / MARCUL / BOYLAT /
      BCNLAT / LIGHTS / BRIDGE; maps S-57 COLOUR codes to OpenCPN sym
      names. Tests in `tests/unit/test_charts_enc.py`.)

- [x] OpenSeaMap reader (`osmium` — the former `pyosmium` 4.x) →
      preprocessed GeoJSON (`app/services/charts_osm.py` — lifts
      `natural=coastline`, `seamark:type=(buoy_*|beacon_*|light_*|wreck|
      rock|obstruction|restricted_area|marine_farm)`. Tests in
      `tests/unit/test_charts_osm.py`.)

- [x] GEBCO reader (`xarray`) → in-memory bathymetry
      (`app/services/gebco.py` — `load_gebco_bbox(path, bbox)` slices a
      netCDF into memory; `GebcoBathymetry.depth(lat, lon)` bilinear
      interpolates, returns `None` over land or outside the slice. Spans
      `charts.load`, counter `bv.charts.cells_loaded{source="gebco"}`.
      Tests in `tests/unit/test_gebco.py`. ChartStore integration lands
      with the remaining chart ingest work.)

- [x] `python -m app.charts fetch --bbox|--region` CLI
      (`app/charts/cli.py` — sync wrapper over `ChartStore.ensure_coverage`;
      exit codes 0/2/3/64; named regions for the common US boating bays.)

- [x] Coverage policy: hard-fail with `CHARTS_NOT_AVAILABLE` on gaps
      (`ChartStore._coverage_gaps` returns the request bbox minus the
      union of loaded source bboxes; planner maps
      `ChartsCoverageError` → `PlannerError("CHARTS_NOT_AVAILABLE")`.)

- [x] Charts spans + metrics (`bv.charts.*` — `ensure_coverage`,
      `fetch`, `load`, `preprocess` spans; `bv.charts.queries{kind}`,
      `cells_loaded`, `fetch_bytes{source}`, `fetch_seconds{source}`,
      `query_duration_seconds{kind}` metrics.)

**Forecast + router + scoring** ✅ (charts use NullChartStore stub)

- [x] `services/forecast_field.py` — cached grid + bilinear/temporal
  
      interpolation

- [x] `services/polars.py` — CSV load + bilinear `(TWA, TWS) → BSP`

- [x] `app/data/polars/cruiser_*.pol` — nominal ORC-derived polars

- [x] `services/router.py` — isochrone kernel, sector pruning,
  
      `fastest` objective, decimation; consults `ChartStore` for
      land / obstacle / restricted / depth checks

- [x] `services/scorer.py` — post-hoc sub-scores + composition

- [x] `services/planner.py` — orchestrates the job's stages, writes
  
      progress

- [x] `GET /voyages/{id}/trace` debug endpoint (scaffolded; trace
      population is progressive)

- [x] Unit tests: polar interp, geodesic math, scorer goldens

- [x] `services/gpx.py` — minimal emitter that fills
  
      `voyages.gpx_blob` during the `finalizing` stage (inlined in
      planner for now; standalone module + full XSD validation is M5)

- [x] Dedicated `bv.router.*` metrics: `bv.router.steps` (histogram),
      `bv.router.propagations_per_step` (histogram),
      `bv.router.wallclock_seconds` (histogram), `bv.router.outcomes`
      (counter, labels: `outcome` ∈ `ok|timeout|no_coverage`,
      `objective`). Spans on `job.routing` / `forecast.prefetch`
      already wired.

- [x] One completed routed candidate end-to-end over `/voyages`

## M3 — Candidate enumeration + multi-objective ✅

*Goal: enumerate the departure grid, route in parallel, rank, return
top-N. `comfortable` and `short_tacks` objectives supported.*

- [x] Prefetch forecast field for voyage bbox × window
- [x] Parallel routing in executor, bounded semaphore
- [x] `comfortable` + `short_tacks` objective functions
- [x] User-supplied polar files via `BoatProfile.polar_path`
- [x] Ranking + top-N with stable tiebreak
- [x] `coverage`, `skipped` fields in response
- [x] Metrics: `bv.voyages.candidates_total`, `candidates_rejected`
- [x] Local-time + night-arrival departure filters

## M4 — Contingencies + navaids in GPX

*Goal: every decision-point rtept has tap-out annotations; risky legs
get isochrone-derived escape-hatch routes; destinations have backup
anchorages; navaids appear in the emitted GPX.*

- [x] `services/contingency.py` — tap-out selector (linear scan; R-tree
      upgrade lands with ChartStore)

- [x] Backup-anchorage selection on the destination rtept

- [x] Escape-hatch re-routing (`plan_escape_hatches` — isochrone from
      each decision point to the nearest refuge POI with tightened
      `max_seas_m`; triggers on `ESCAPE_SEAS_M` / `ESCAPE_WIND_KTS`
      downstream env breaches)

- [x] Discrete Fréchet check to suppress trivial re-routes
      (`geo.discrete_frechet_nm`, cutoff `ESCAPE_DIVERGENCE_NM=2.0`)

- [x] Escape-hatch routes emitted as additional `<rte>` elements
      (`bv:contingencyKind=escape_hatch_route`, `bv:parentRtept`,
      `bv:trigger`)

- [x] Navaids from `ChartStore.navaids_in(bbox)` emitted as `<wpt>`
      within `BV_NAVAID_BBOX_PAD_NM` of any route leg
      (`app/services/gpx.py::_navaids_for_routes` — bbox is the padded
      envelope of every candidate's rtepts; dedup by `(lat,lon)`
      rounded to 6 dp. Covered by the real-mode integration test
      `test_real_chart_store_runs_end_to_end`.)

- [x] Metrics: `bv.contingencies.emitted{kind}`
      (`kind ∈ backup_destination|tap_out|escape_hatch_route`)

## M5 — GPX polish & validation

*Goal: the GPX that M2's minimal emitter already produces passes the
GPX 1.1 XSD, round-trips losslessly through inbound parsing, and
renders cleanly in OpenCPN with primary + contingencies + navaids.*

- [x] Upgrade `services/gpx.py` from M2 stub: full `bv:` extensions
      round-trip on emit via gpxpy. Every candidate primary + escape
      hatch serializes through `gpxpy.gpx.GPX` with `bv:` elements as
      `ET.Element` children of `extensions`; re-parsing with gpxpy
      preserves every attribute (test:
      `test_gpx_round_trips_through_gpxpy`). Inbound-parse parity lands
      with a future voyages ingest endpoint; foreign namespaces
      already pass through gpxpy verbatim.
- [x] Per-candidate and master file endpoints
      (`GET /voyages/{id}/gpx?candidate=<rank>` — filters primary +
      its escape-hatch `<rte>`s; 404 CANDIDATE_NOT_FOUND on miss)
- [x] Deterministic element ordering (doc 09) — candidate primaries
      emit in ascending `bv:candidate/@rank`, escape-hatch `<rte>`s
      follow their parent; test asserts the invariant
- [x] Validation test against GPX 1.1 XSD
      (`test_gpx_validates_against_gpx_1_1_xsd` — lxml + bundled
      `tests/fixtures/gpx/gpx-1.1.xsd`; validates both the master and
      per-candidate files. `bv:` extensions ride through the XSD's
      `xsd:any namespace="##other" processContents="lax"`.)
- [ ] Manual verification in OpenCPN

## M6 — NL summary (LLM) ✅

*Goal: each surfaced candidate has a 1–3 sentence LLM-generated recap
(Claude Haiku 4.5) with a templated fallback for offline operation.*

- [x] Dep: `anthropic>=0.45`

- [x] `BV_SUMMARY_*` settings (mode, model, temperature, timeout,
  
      cache TTL) in `app/config.py`

- [x] `services/summary.py` — `digest_candidate()` (pure), Anthropic
  
      caller, fallback templater, cache lookup

- [x] System prompt + few-shot examples at
  
      `app/data/prompts/summary_system.md`

- [x] SQLite `summary_cache` table keyed by
  
      `sha256(digest + prompt_version + model)`

- [x] Observability: `summary.render` spans + `bv.summary.*` metrics
  
      (tokens, duration, source, failures)

- [x] Unit: golden tests on `digest_candidate()`; exact-match on
  
      fallback template

- [x] Contract tests on LLM output (length, mentions, no markdown)

- [x] `conftest.py` sets `BV_SUMMARY_MODE=fallback_only` by default;
  
      live-LLM path exercised only in a gated CI job

## M7 — Polish & offline hardening

*Goal: cold-start voyage with network disabled (post-prefetch) end-to-
end.*

- [x] Stale-while-error in the cache wrapper (returns `CacheResult.stale=True`
      when upstream fails but a prior cache row exists — tested in
      `tests/unit/test_cache.py`)

- [x] Prune task for expired cache rows (`services/cache_pruner.py`;
      hourly `run_forever()` registered on the FastAPI lifespan)

- [x] Request-level input validation for `INVALID_WINDOW`
      (start_at ≥ end_at, window > 14 days, origin == destination)

- [x] `503` with cached-data hint when all candidates fail offline
      (new `OFFLINE_NO_ROUTE` error code raised by the planner when
      forecast served stale AND no candidates survived; `GET
      /voyages/{id}` maps it to 503 and surfaces `coverage` in the
      body)

- [x] Surface `stale_at` on the voyage's `bv:coverage` when any upstream
      cache served stale (`ForecastField.stale_at` bubbles into
      `coverage_json.forecast_stale_at` and `<bv:coverage
      forecastStaleAt="..."/>` in the emitted GPX)

- [x] Grafana dashboard JSON checked in
      (`ops/grafana/better-voyage.json` — panels for jobs, cache,
      router, contingencies; targets the Prometheus datasource the
      `otel-lgtm` image provisions. Import via Grafana UI; automated
      provisioning lands with ops docs)

- [x] Ops docs (`ops/README.md` — runbook, error-code reference,
      configuration cheat sheet, Grafana import steps)

## Stretch (post-MVP)

- Pareto / multi-objective routing (currently single-objective per run)
- GRIB ingestion for offshore routes beyond Open-Meteo coverage
- Small web UI (HTMX / Leaflet) for picking origin/destination
- User accounts + history
- LLM-polished summaries behind a flag
- "Departure diary" — record actuals, compare to predicted candidate,
  feed a polar-calibration dataset

## Exit criteria for "MVP complete"

- M0 → M7 all green
- One skipper plans a real passage with `better-voyage`, the GPX loads
  cleanly in OpenCPN, and the predicted ETA matches within 15% of the
  actual passage
