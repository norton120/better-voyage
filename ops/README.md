# better-voyage — ops guide

Operator-facing companion to the design docs in [`plan/`](../plan/). If
you're hunting a bug, reach for this first.

## Running the service

| Mode                 | Command                                                                | Notes                                               |
| -------------------- | ---------------------------------------------------------------------- | --------------------------------------------------- |
| Local dev (uv)       | `uv run uvicorn app.main:app --reload`                                 | SQLite at `./data/better-voyage.db`                 |
| Docker               | `docker compose up --build`                                            | `app` only, OTel exporter = `console`               |
| With observability   | `BV_OTEL_EXPORTER=otlp docker compose --profile observability up`      | Bundled Grafana + Tempo + Loki + Prometheus         |

Surfaces:

- `/` — HTMX + Leaflet UI (click map to pick endpoints, fill form, submit).
- `/docs` — OpenAPI / Swagger, hits the JSON API directly.
- `/health` — liveness probe.

### Chart ingest prerequisites

`POST /voyages` is blocking on the real ChartStore (plan/15) —
`BV_CHART_STORE_MODE=real` is the default.

**GEBCO bathymetry** is auto-downloaded on first startup to
`$BV_CHARTS_DIR/gebco/GEBCO_2024_sub_ice_topo.nc` (~8 GB, ~10–20 min
on a fast connection). The UI renders a "preparing charts" banner
while the download runs and reloads itself into the planner page once
complete. Subsequent starts are instant (the file is reused). To
override the default behavior:

- **Pre-stage the file** and point the service at it — skip the
  auto-download entirely:
  ```bash
  export BV_GEBCO_PATH=/abs/path/to/gebco_2024_sub_ice_topo.nc
  ```
  In Docker, mount the netCDF and set the env var in the `app`
  service; the auto-download is skipped when the path exists.
- **Disable auto-download** (air-gapped deploys):
  ```bash
  export BV_GEBCO_AUTO_DOWNLOAD=false
  # + pre-stage the file manually, see above
  ```
- **Point at a different source** (e.g. a GEBCO mirror or a newer
  yearly grid):
  ```bash
  export BV_GEBCO_DOWNLOAD_URL=https://...
  ```

**Pre-seed chart cells** for your cruising area (optional — the
first voyage otherwise pays the full NOAA ENC + Overpass download
on-demand):
```bash
uv run python -m app.charts fetch --region chesapeake
# or: --bbox lat_min,lon_min,lat_max,lon_max
```
Exit code 0 = coverage sealed, 2 = `CHARTS_NOT_AVAILABLE`, 3 =
`CHARTS_FETCH_FAILED`, 64 = bad CLI usage.

For API-exploration only (no real routing), set
`BV_CHART_STORE_MODE=null` — the planner uses a stub that treats the
whole planet as navigable water. Never run production like this.

## Configuration

All runtime knobs are `BV_*` env vars (see `app/config.py`). The ones
operators usually tune:

| Var                       | Default                                          | Why you'd change it                                               |
| ------------------------- | ------------------------------------------------ | ----------------------------------------------------------------- |
| `BV_DATABASE_URL`         | `sqlite+aiosqlite:///./data/better-voyage.db`    | Point at a shared volume or a non-default path.                   |
| `BV_CACHE_DIR`            | `./data/cache`                                   | Spill directory for oversized cache artifacts.                    |
| `BV_FORECAST_CACHE_TTL_S` | `10800` (3 h)                                    | Extend during offline operation to survive long upstream outages. |
| `BV_TIDE_CACHE_TTL_S`     | `86400` (24 h)                                   | Same reasoning; tide predictions are stable for days.             |
| `BV_MAX_CONCURRENT_JOBS`  | `2`                                              | Raise when routing CPU is underutilized.                          |
| `BV_SUMMARY_MODE`         | `llm`                                            | Set `fallback_only` to skip Anthropic entirely.                   |
| `BV_OTEL_EXPORTER`        | `console`                                        | `otlp` to ship to the compose lgtm stack, `none` to disable.      |
| `BV_OTEL_ENDPOINT`        | `http://localhost:4318`                          | OTLP HTTP receiver.                                               |
| `BV_LOG_LEVEL`            | `INFO` (`DEBUG` in compose)                      | Structlog root level.                                             |
| `BV_CHART_STORE_MODE`     | `real`                                           | `null` for API smoke-tests without chart ingest (no real routing).|
| `BV_GEBCO_PATH`           | unset                                            | Override path to the GEBCO netCDF. Defaults to `$BV_CHARTS_DIR/gebco/GEBCO_2024_sub_ice_topo.nc` (auto-downloaded on first start). |
| `BV_GEBCO_DOWNLOAD_URL`   | BODC mirror                                      | Source URL for the first-boot GEBCO download; change when GEBCO publishes a new yearly grid. |
| `BV_GEBCO_AUTO_DOWNLOAD`  | `true`                                           | `false` in air-gapped deploys — then pre-stage the file at `BV_GEBCO_PATH` yourself.         |
| `BV_CHARTS_DIR`           | `./data/charts`                                  | ENC / OSM cache root; pre-seed with `python -m app.charts fetch`. |
| `BV_CHARTS_MAX_AGE_DAYS`  | `90`                                             | Refresh cadence for cached ENC cells and OSM extracts.            |
| `BV_SHALLOW_CUTOFF_M`     | `2.0`                                            | DEPARE polygons below this depth are treated as shallow hazards.  |
| `BV_NAVAID_BBOX_PAD_NM`   | `2.0`                                            | How far off the routed legs to pull navaids into the emitted GPX. |
| `BV_TIDE_MODULATED_DEPTH` | `false`                                          | Flip to `true` once the tide interpolator is validated (plan/15). |

## Data & cache

SQLite is authoritative for voyages, forecast cache, tide cache, and
stations cache. A background task in `services/cache_pruner.py` sweeps
expired cache rows hourly on the FastAPI lifespan.

To force a cold start:

```bash
rm -rf data/better-voyage.db data/cache
```

To inspect cache state interactively:

```bash
sqlite3 data/better-voyage.db \
  "SELECT source, COUNT(*), MIN(fetched_at), MAX(expires_at)
     FROM forecast_cache GROUP BY 1"
```

## Grafana dashboard

Provisioned via `ops/grafana/better-voyage.json`. Quickest import:

1. Start the observability profile (see table above).
2. Grafana UI → Dashboards → New → Import.
3. Upload `ops/grafana/better-voyage.json`, select the Prometheus
   datasource (auto-provisioned in the `otel-lgtm` image).

Panels cover four rows: **Jobs** (throughput, stage durations,
failures, transitions), **Upstream & Cache** (lookups, hit ratio),
**Router** (outcomes, wallclock, steps, propagations, rejected
candidates), **Contingencies & Summary** (emitted by kind, summary
source mix).

## Runbook

Symptom → where to look:

| Symptom                                   | Where to look                                                                                                         |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `POST /voyages` → `409 VOYAGE_IN_PROGRESS`| Single-voyage retention — there's a live one. Retry with `?force=true` or wait it out.                               |
| `POST /voyages` → `404 BOAT_PROFILE_NOT_FOUND` | No matching row in `boat_profiles`. Seed with `POST /boat-profiles`.                                             |
| `GET /voyages/{id}` → `503 OFFLINE_NO_ROUTE` | Routing failed with stale forecast. Check `bv.cache.lookups{result="stale"}`; extend `BV_FORECAST_CACHE_TTL_S` or refresh upstream. Response body carries `voyage.coverage.forecast_stale_at`. |
| Voyage `failed` with `ROUTE_BLOCKED`      | Every candidate hit a router failure. Drill into `bv.voyages.candidates_rejected{reason}` for the breakdown.         |
| Voyage `failed` with `FORECAST_UNAVAILABLE` | Prefetch raised with no cache to fall back on. Check upstream reachability; look at `bv.cache.lookups{result="error"}`. |
| Voyage `failed` with `WORKER_RESTARTED`   | Process restart during a live job. The crash-recovery sweep (`jobs.sweep_crashed`) marks the row; rerun to replan.  |
| Voyage stuck in `forecast_prefetching`    | Open-Meteo slow or misbehaving. Look at the `forecast.prefetch` span for offending `(lat, lon)` calls.               |
| Voyage `failed` with `CHARTS_NOT_AVAILABLE` | ENC + OSM can't cover the bbox, or GEBCO isn't staged yet. Confirm the UI preparing-banner cleared (or that `BV_GEBCO_PATH` exists); pre-seed ENC with `python -m app.charts fetch --bbox ...`. |
| Voyage `failed` with `CHARTS_FETCH_FAILED`  | Transient network error hitting NOAA ENC catalog / Overpass. Retry; if persistent, inspect the `charts.fetch` span for the offending URL. |
| Voyage stuck in `charts_fetching`         | First-run fetch of a big region. Typical latency 30 s – few minutes per ~1°×1° region. Pre-seed via CLI to eliminate the wait. |
| Voyage stuck in `routing`                 | `bv.router.wallclock_seconds`, `bv.router.steps` — often paired with `bv.router.outcomes{outcome="timeout"}` spikes. |
| NL summary always templated               | `bv.summary.rendered_total{source="fallback"}` dominant. Check Anthropic reachability or set `BV_SUMMARY_MODE=fallback_only` to stop trying. |
| Cache pruner never runs                   | Lifespan startup log for `cache_pruner.started`. Pruner runs hourly via `run_forever`.                              |

Error-code reference (what the planner raises):

| Code                       | Stage                     | Meaning                                                                                 |
| -------------------------- | ------------------------- | --------------------------------------------------------------------------------------- |
| `INVALID_WINDOW`           | validation                | Request-shape problem (window duration, endpoint collision, start ≥ end).               |
| `BOAT_PROFILE_NOT_FOUND`   | routing                   | The named profile doesn't exist.                                                        |
| `INVALID_BOAT`             | routing                   | Polar file load failed.                                                                  |
| `CHARTS_NOT_AVAILABLE`     | charts_fetching           | No combination of NOAA ENC + OSM covers the bbox, or GEBCO is unconfigured / missing.   |
| `CHARTS_FETCH_FAILED`      | charts_fetching           | Upstream NOAA or Overpass error after retries. Retry later or pre-seed via the CLI.     |
| `FORECAST_UNAVAILABLE`     | forecast_prefetching      | Prefetch raised; no usable cache rows to fall back on.                                  |
| `ROUTE_BLOCKED`            | routing                   | Every candidate failed and no upstream was stale — suggests the voyage is infeasible.   |
| `OFFLINE_NO_ROUTE`         | routing                   | Every candidate failed *and* at least one upstream served stale — likely offline gap.   |
| `INTERNAL_ERROR`           | any                       | Catch-all for unexpected failures; the span and log carry detail.                       |
| `WORKER_RESTARTED`         | crash recovery (any live) | Process restart caught the job mid-run; voyage marked failed, resubmit.                 |
| `ROUTE_NO_COVERAGE`        | routing (per candidate)   | Emitted by the router when the frontier collapses under forecast gaps. Not terminal at voyage level unless all candidates hit this. |
| `ROUTE_TIMEOUT`            | routing (per candidate)   | Step budget exhausted before reaching destination. Not terminal at voyage level.        |

## Correlation IDs

Every request honors an inbound `X-Request-ID` header (ULID generated
when absent). The id flows through structlog contextvars and is set as
an attribute on the root `voyage.job` span, so job logs stay tied to
the submitting request even after the HTTP response closes.

## Source-of-truth lookups

- Design: [`plan/`](../plan/).
- Roadmap / status: [`plan/13-roadmap.md`](../plan/13-roadmap.md).
- Observability spec: [`plan/14-observability.md`](../plan/14-observability.md).
- API surface: [`plan/10-api.md`](../plan/10-api.md).
