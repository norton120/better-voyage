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

**Async job infrastructure** (see doc 15)

- [ ] `services/jobs.py` — `JobRegistry`, async task lifecycle,
  
      scheduler coroutine, crash-recovery sweep on startup

- [ ] `voyages` table status / progress / error columns (doc 11)

- [ ] `POST /voyages` → `202` with voyage id

- [ ] `GET /voyages/{id}` returns status + progress + voyage (when done)

- [ ] `POST /voyages/{id}/cancel`

- [ ] Idempotency via `inputs_hash`

- [ ] Jobs spans + metrics (`bv.jobs.*`)

**Chart ingest** (see doc 03, Part 2)

- [ ] Heavy-lift deps: `numpy`, `scipy`, `pyproj`, `shapely`,
  
      `pyogrio`, `pyosmium`, `netCDF4`, `xarray`, `geopandas`

- [ ] `Dockerfile` installs `gdal-bin libgdal-dev libspatialite-dev`

- [ ] `services/charts.py` — `ChartStore` with `ensure_coverage`,
  
      per-bbox lock, preprocessing pipeline

- [ ] NOAA ENC reader (S-57 via `pyogrio`) → preprocessed GeoJSON

- [ ] OpenSeaMap reader (`pyosmium`) → preprocessed GeoJSON

- [ ] GEBCO reader (`xarray`) → in-memory bathymetry

- [ ] `python -m app.charts fetch --bbox|--region` CLI

- [ ] Coverage policy: hard-fail with `CHARTS_NOT_AVAILABLE` on gaps

- [ ] Charts spans + metrics (`bv.charts.*`)

**Forecast + router + scoring**

- [ ] `services/forecast_field.py` — cached grid + bilinear/temporal
  
      interpolation, coroutine-local cell cache

- [ ] `services/polars.py` — CSV load + bilinear `(TWA, TWS) → BSP`

- [ ] `app/data/polars/cruiser_*.pol` — nominal ORC-derived polars

- [ ] `services/router.py` — isochrone kernel, sector pruning,
  
      `fastest` objective, decimation; consults `ChartStore` for
      land / obstacle / restricted / depth checks

- [ ] `services/scorer.py` — post-hoc sub-scores + composition

- [ ] `services/planner.py` — orchestrates the job's stages, writes
  
      progress, emits `PlanTrace`

- [ ] `GET /voyages/{id}/trace` debug endpoint (populated progressively)

- [ ] Unit tests: polar interp, geodesic math, scorer golden table,
  
      chart crossings against synthetic geometry, job state machine

- [ ] `services/gpx.py` — minimal emitter that fills
  
      `voyages.gpx_blob` during the `finalizing` stage (full XSD
      validation + round-trip polish is M5)

- [ ] Router spans + metrics wired (`bv.router.*`)

- [ ] One completed routed candidate end-to-end over `/voyages`

## M3 — Candidate enumeration + multi-objective

*Goal: enumerate the departure grid, route in parallel, rank, return
top-N. `comfortable` and `short_tacks` objectives supported.*

- [ ] Prefetch forecast field for voyage bbox × window
- [ ] Parallel routing in executor, bounded semaphore
- [ ] `comfortable` + `short_tacks` objective functions
- [ ] User-supplied polar files via `BoatProfile.polar_path`
- [ ] Ranking + top-N with stable tiebreak
- [ ] `coverage`, `skipped` fields in response
- [ ] Metrics: `bv.voyages.candidates_total`, `candidates_rejected`

## M4 — Contingencies + navaids in GPX

*Goal: every decision-point rtept has tap-out annotations; risky legs
get isochrone-derived escape-hatch routes; destinations have backup
anchorages; navaids appear in the emitted GPX.*

- [ ] `services/contingency.py` — tap-out selector with POI R-tree

- [ ] Backup-anchorage selection on the destination rtept

- [ ] Escape-hatch re-routing (isochrone from decision point with
  
      tightened constraints / alternate endpoint)

- [ ] Discrete Fréchet check to suppress trivial re-routes

- [ ] Escape-hatch routes emitted as additional `<rte>` elements

- [ ] Navaids from `ChartStore.navaids_in(bbox)` emitted as `<wpt>`
  
      within `NAVAID_BBOX_PAD_NM` of any route leg

- [ ] Metrics: `bv.contingencies.emitted{kind}`

## M5 — GPX polish & validation

*Goal: the GPX that M2's minimal emitter already produces passes the
GPX 1.1 XSD, round-trips losslessly through inbound parsing, and
renders cleanly in OpenCPN with primary + contingencies + navaids.*

- [ ] Upgrade `services/gpx.py` from M2 stub: full `bv:` extensions
  
      round-trip on ingest + emit (foreign namespaces preserved)
- [ ] Per-candidate and master file endpoints (`?candidate=<rank>`)
- [ ] Deterministic element ordering (doc 09) for meaningful test diffs
- [ ] Validation test against GPX 1.1 XSD
- [ ] Manual verification in OpenCPN

## M6 — NL summary (LLM)

*Goal: each surfaced candidate has a 1–3 sentence LLM-generated recap
(Claude Haiku 4.5) with a templated fallback for offline operation.*

- [ ] Dep: `anthropic>=0.45`

- [ ] `BV_SUMMARY_*` settings (mode, model, temperature, timeout,
  
      cache TTL) in `app/config.py`

- [ ] `services/summary.py` — `digest_candidate()` (pure), Anthropic
  
      caller, fallback templater, cache lookup

- [ ] System prompt + few-shot examples at
  
      `app/data/prompts/summary_system.md`

- [ ] SQLite `summary_cache` table keyed by
  
      `sha256(digest + prompt_version + model)`

- [ ] Observability: `summary.render` spans + `bv.summary.*` metrics
  
      (tokens, duration, source, failures)

- [ ] Unit: golden tests on `digest_candidate()`; exact-match on
  
      fallback template

- [ ] Contract tests on LLM output (length, mentions, no markdown)

- [ ] `conftest.py` sets `BV_SUMMARY_MODE=fallback_only` by default;
  
      live-LLM path exercised only in a gated CI job

## M7 — Polish & offline hardening

*Goal: cold-start voyage with network disabled (post-prefetch) end-to-
end.*

- [ ] Stale-while-error everywhere

- [ ] `503` with cached-data hint when all candidates fail offline

- [ ] Prune task for expired cache rows

- [ ] Grafana dashboard JSON checked in (provisioned on the lgtm
  
      stack)

- [ ] Ops docs

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
