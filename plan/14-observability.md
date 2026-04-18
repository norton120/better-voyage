# 14 — Observability

**Status:** draft

A debuggable planner is non-negotiable. When a voyage comes back weird
("why is the Tuesday candidate so low?", "why did the charts fetch
fail?") we need to answer from logs / traces / metrics alone, without
re-running under a debugger. Wired in **from day one** (M0), not
bolted on later.

## Stack

OpenTelemetry end-to-end. One SDK produces logs, traces, and metrics;
export target is switchable per-env:

- **Default (dev):** console exporters. Zero infra; `docker compose up`
  works out of the box.
- **Opt-in (local):** `grafana/otel-lgtm` single-container backend —
  Grafana + Tempo + Loki + Prometheus. Enable via compose profile (see
  doc 02 and `compose.yaml`).
- **Future (shared):** point `BV_OTEL_ENDPOINT` at any OTLP-compatible
  collector.

## Signals

### Logs

- `structlog` with JSON output in non-dev; console-pretty renderer in
  dev.
- Every event carries:
  - `timestamp` (ISO-8601 UTC)
  - `level`
  - `event` — short stable key (`job.submitted`, `job.stage_changed`,
    `charts.fetch.begin`, `router.isochrone.step`,
    `candidate.rejected`, `gpx.emit.done`, ...)
  - `trace_id`, `span_id` — injected from the active OTel span.
  - `correlation_id` — honored from inbound `X-Request-ID`, generated
    otherwise. Returned in the response header.
  - `voyage_id` — set via `structlog.contextvars` once the voyage is
    created; appears on every subsequent log line in the job.
- Never log secrets. Upstream URLs logged; bodies only at `DEBUG`.

### Traces

Planning is a background job (doc 15). The root span is the **job**,
not the HTTP request. The HTTP handlers produce short auxiliary
spans around the row writes.

Trace topology for a voyage:

```
voyage.job                              (root span — lives for the job's full duration)
├── job.queued_for_ms                   (wait time in queue before dispatch)
├── job.charts_fetching
│   └── charts.ensure_coverage
│       ├── charts.fetch{source=noaa_enc}
│       ├── charts.fetch{source=osm}
│       └── charts.fetch{source=gebco}
├── job.charts_preprocessing
│   └── charts.preprocess               (one per cell / extract, first-load only)
├── job.forecast_prefetching
│   ├── open_meteo.get_marine           (N — one per grid cell)
│   ├── cache.put                       (N)
│   ├── noaa.list_stations
│   └── noaa.get_predictions            (N)
├── job.routing
│   ├── enumerate_departures
│   ├── router.plan_candidate           (one span per departure t)
│   │   ├── router.isochrone_step       (sampled — >200 steps sampled at 10%)
│   │   └── router.decimate
│   └── ... × N
├── job.scoring
│   └── score_candidates
├── job.finalizing
│   ├── contingency.derive              (top-N only)
│   │   ├── contingency.backup_anchorages
│   │   ├── contingency.tapouts
│   │   └── router.plan_candidate       (nested — one per escape-hatch re-route)
│   ├── render_summaries
│   ├── persist_voyage
│   └── emit_gpx
└── job.status_transition               (terminal event — done / failed / cancelled)
```

Attributes set explicitly:

- **`voyage.job`** (root): `bv.voyage_id`, `bv.inputs_hash`,
  `bv.correlation_id`, `bv.objective`, `bv.job.final_status`,
  `bv.candidates.enumerated`, `bv.candidates.routed`,
  `bv.candidates.returned`.
- **`job.<stage>`**: `bv.job.stage`, `bv.job.stage_wallclock_seconds`.
- **`charts.fetch`**: `bv.charts.source`, `bv.charts.bytes`,
  `bv.charts.cells_downloaded`.
- **Upstream spans** (`open_meteo.*`, `noaa.*`): `http.method`,
  `http.url`, `http.status_code`, `bv.cache.outcome` = `hit` / `miss`
  / `stale`.
- **`router.plan_candidate`**: `bv.candidate.depart_at`,
  `bv.router.dt_s`, `bv.router.n_steps_executed`,
  `bv.router.n_frontier_points_total`, `bv.router.arrival_dist_nm`,
  `bv.router.outcome` ∈ `ok` / `timeout` / `no_coverage` / `blocked`
  / `limit_exceeded`; on success `bv.candidate.rank` and
  `bv.candidate.score` (backfilled after scoring).
- **`contingency.derive` nested `router.plan_candidate`**:
  `bv.contingency.parent_rtept`, `bv.contingency.kind`,
  `bv.contingency.trigger`.

### Metrics

| Metric                            | Type      | Attributes                                                                                  |
| --------------------------------- | --------- | ------------------------------------------------------------------------------------------- |
| `bv.jobs.submitted`               | counter   |                                                                                             |
| `bv.jobs.completed`               | counter   | `status` ∈ `done` / `failed` / `cancelled`                                                  |
| `bv.jobs.duration_seconds`        | histogram | `status`                                                                                    |
| `bv.jobs.queue_depth`             | gauge     |                                                                                             |
| `bv.jobs.stage_duration_seconds`  | histogram | `stage`                                                                                     |
| `bv.jobs.stage_transitions`       | counter   | `from`, `to`                                                                                |
| `bv.jobs.failures`                | counter   | `error_code`                                                                                |
| `bv.charts.queries`               | counter   | `kind` ∈ `land` / `obstacle` / `restricted` / `depth` / `navaid`                            |
| `bv.charts.cells_loaded`          | gauge     |                                                                                             |
| `bv.charts.fetch_bytes`           | counter   | `source` ∈ `noaa_enc` / `osm` / `gebco`                                                     |
| `bv.charts.fetch_seconds`         | histogram | `source`                                                                                    |
| `bv.upstream.requests`            | counter   | `source`, `outcome` (`ok` / `error` / `timeout`)                                            |
| `bv.upstream.duration_seconds`    | histogram | `source`                                                                                    |
| `bv.cache.lookups`                | counter   | `source`, `outcome` (`hit` / `miss` / `stale`)                                              |
| `bv.router.steps`                 | histogram | per-candidate step count                                                                    |
| `bv.router.propagations_per_step` | histogram |                                                                                             |
| `bv.router.wallclock_seconds`     | histogram | per-candidate router run time                                                               |
| `bv.router.outcomes`              | counter   | `outcome`                                                                                   |
| `bv.voyages.candidates_total`     | histogram | count per voyage                                                                            |
| `bv.voyages.candidates_rejected`  | counter   | `reason` ∈ `route_blocked` / `route_timeout` / `route_limit_exceeded` / `route_no_coverage` |
| `bv.scoring.component`            | histogram | `component` ∈ `wind` / `waves` / `swell` / `current` / `tide` / `comfort` / `smg`           |
| `bv.scoring.total`                | histogram |                                                                                             |
| `bv.contingencies.emitted`        | counter   | `kind`                                                                                      |

Histogram buckets use OTel defaults for MVP; tune once real
distributions emerge.

## Correlation

- Honor `X-Request-ID` inbound; generate ULID if absent.
- Stored in `structlog.contextvars` for the request lifetime.
- Returned in the `X-Request-ID` response header.
- Also set as `voyage.job` root-span attribute `bv.correlation_id`.
- When `POST /voyages` dispatches a job, it passes its correlation id
  into the job's `structlog.contextvars` so **all job logs carry the
  same correlation_id as the submit request** — even though the job
  outlives the HTTP response.

## Plan audit trail

OTel traces tell us **what happened**. A domain audit trail tells us
**why the planner chose this**.

For every voyage we persist a `PlanTrace`:

- Normalized request inputs.
- Charts coverage summary (cells loaded, gaps).
- Enumerated departure grid.
- For each candidate (including rejected ones):
  - `depart_at`, router outcome, failure reason if any.
  - On failure: last isochrone reached and its frontier points.
  - On success: per-leg `LegEnvironment`, sub-scores, weights, leg
    total, candidate total.
- Final ranking + tiebreak reasoning.
- Contingencies attempted (including Fréchet-rejected re-routes).

Stored as JSON alongside the voyage GPX in SQLite (doc 11); exposed
via `GET /voyages/{id}/trace` (doc 10). **Populated progressively
while the job runs** — a client can fetch the trace during `routing`
and see partial candidate results.

Separate from OTel tracing on purpose: OTel spans don't survive past
the collector's retention, and we want audit trails to outlive the
trace store.

## Configuration

All via `BV_*` env vars (see `app/config.py`):

| Var                    | Default                     | Notes                        |
| ---------------------- | --------------------------- | ---------------------------- |
| `BV_OTEL_EXPORTER`     | `console`                   | `console` / `otlp` / `none`  |
| `BV_OTEL_ENDPOINT`     | `http://localhost:4318`     | OTLP HTTP receiver           |
| `BV_OTEL_SAMPLE_RATIO` | `1.0`                       | head sampling (parent-based) |
| `BV_OTEL_SERVICE_NAME` | `better-voyage`             |                              |
| `BV_LOG_LEVEL`         | `INFO` (`DEBUG` in compose) |                              |

## Instrumentation libraries

- `opentelemetry-instrumentation-fastapi` — request span + route
  attributes (on HTTP handlers; the job span is created manually
  because it outlives the request).
- `opentelemetry-instrumentation-httpx` — every upstream call
  becomes a child span automatically.
- `opentelemetry-instrumentation-sqlalchemy` — DB statement + timing.
- `opentelemetry-instrumentation-logging` — injects `trace_id` /
  `span_id` into stdlib `LogRecord`; our structlog processor mirrors
  this for structlog events.

## Testing

- Tests default `BV_OTEL_EXPORTER=none` (set in `conftest.py`).
- Smoke test per exporter mode catches wiring regressions.
- Job lifecycle tests assert spans exist at each stage, and that a
  failed job emits `voyage.job.final_status=failed` on the root span.

## Runbook hooks

| Symptom                                | Where to look                                                                                                   |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Job stuck in `charts_fetching`         | `charts.fetch` spans; `bv.charts.fetch_seconds{source}` histogram                                               |
| Job failed with `CHARTS_NOT_AVAILABLE` | `PlanTrace.charts_coverage.gaps`; `bv.jobs.failures{error_code=CHARTS_NOT_AVAILABLE}`                           |
| Job failed with `WORKER_RESTARTED`     | Lifespan log `event=jobs.reaped_stale` on the restart boundary                                                  |
| All candidates rejected                | `bv.voyages.candidates_rejected{reason}`; drill into `PlanTrace`                                                |
| Specific candidate missing             | `PlanTrace` for that voyage → candidate's `router.outcome`                                                      |
| Slow plan                              | `bv.jobs.duration_seconds`, `bv.jobs.stage_duration_seconds{stage}`, `bv.router.wallclock_seconds`, flame graph |
| Stale coverage                         | `bv.cache.lookups{outcome=stale}` + `voyage.coverage.stale_at`                                                  |
| Escape hatch not emitted               | `PlanTrace.contingencies_attempted` — look for Fréchet-rejected entries                                         |
| Bad GPX                                | Log event `gpx.emit.validation_error` with offending element                                                    |

## Notes

- keep metrics aggregated with a`source` attribute (Aggregated — low cardinality.)
  
  
