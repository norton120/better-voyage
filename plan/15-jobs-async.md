# 15 — Async jobs

**Status:** draft

Voyage planning is not a request/response operation. A cold-charts plan
can take minutes (chart download + preprocess) to tens of minutes
(large candidate grid + chart fetch). We run planning as a **background
job**; the HTTP layer only accepts work, dispatches it, and reports
progress.

## Why async

- **Chart fetch** (doc 03): first-run NOAA ENC + OpenSeaMap + GEBCO
  ingest over a new cruising area is seconds to minutes, sometimes
  longer on a slow link.
- **Forecast prefetch**: wide bbox × 7-day window — seconds, bounded
  by upstream concurrency.
- **Routing**: 1–2 s per candidate × up to 168 candidates = minutes.
- **Cumulative**: a cold new-region voyage can exceed 15 minutes.
  No HTTP client wants to hold that open; no user wants to re-submit
  on a proxy timeout.

Earlier drafts said "run plans in the foreground with extended
request/response timeouts." That was wrong; undoing it.

## Job lifecycle

A voyage **is** a job. There is no separate job table — the
`voyages` row carries the state machine (doc 11).

```
queued
  ↓
charts_fetching              (skippable if cache covers the bbox)
  ↓
charts_preprocessing         (first-load only per cell)
  ↓
forecast_prefetching
  ↓
routing
  ↓
scoring
  ↓
finalizing                   (contingencies, summaries, GPX emit)
  ↓
done
```

Terminal failure states reachable from any live stage: `failed`,
`cancelled`.

## Progress shape

Each stage publishes a progress blob onto the voyage row:

```json
{
  "stage":  "routing",
  "pct":    0.48,
  "detail": "80 / 168 candidates routed",
  "eta_s":  120
}
```

`pct` is per-stage (0–1). A UI wanting overall progress combines
stage weights × per-stage pct (weights declared in `app/config.py`).

Progress writes are **rate-limited**: at most every 2 s or 5 pct
change, whichever comes first. Keeps SQLite WAL churn sane on long
runs.

## Implementation

For a local-first single-container app, a proper task queue
(Redis + arq / dramatiq) is overkill. We use **in-process asyncio
tasks + SQLite-backed state**:

```
services/jobs.py
  class JobRegistry:
      async def submit(req: VoyageRequest) -> voyage_id
      async def cancel(voyage_id) -> bool
      def get(voyage_id) -> Voyage | None       # reads from DB

services/planner.py
  async def run_job(voyage_id) -> None:
      # owns the stage progression; catches exceptions,
      # maps to error codes, writes terminal state.
```

Each `submit` creates an `asyncio.Task` owned by the registry. The
task writes progress to SQLite; HTTP handlers read from SQLite, never
from the in-memory registry. **The DB is the source of truth.**

Concurrency knobs:

- `BV_MAX_CONCURRENT_JOBS` (default 2) — hard cap on running jobs.
- Excess submissions remain `queued` until a slot frees.
- A scheduler coroutine, started in the FastAPI lifespan, pulls the
  oldest `queued` row when a slot opens.

## De-duplication

Two voyages requesting the same chart bbox simultaneously must not
trigger two identical fetches. `ChartStore.ensure_coverage` takes a
per-bbox `asyncio.Lock`; the second waiter blocks on the first fetch
then proceeds. Same pattern for `ForecastField` prefetch keyed by
normalized bbox + window.

Cross-process deduplication (not applicable yet — single process):
defer until we grow out of a single worker.

## Crash recovery

On process start, FastAPI lifespan scans for voyages in live stages
(`charts_fetching`, `charts_preprocessing`, `forecast_prefetching`,
`routing`, `scoring`, `finalizing`, `cancelling`) and marks them
`failed` with `error.code = WORKER_RESTARTED`. Users retry by
POSTing again — idempotency via `inputs_hash` reuses cached charts
and forecasts, so a retry is much faster than the first attempt.

No resumable jobs in MVP — each process restart is a full retry.
If that proves painful (chart fetches repeatedly dying mid-download
on a flaky link), we add per-stage resumption later. The caches
survive restarts regardless, so retries only redo the incomplete
stage's unfetched work.

## Cancellation

`POST /voyages/{id}/cancel`:

1. Mark row `status=cancelling` atomically.
2. Call `.cancel()` on the owning `asyncio.Task`.
3. Task catches `CancelledError`, writes `status=cancelled`,
   re-raises.
4. Partial chart / forecast caches are kept for future jobs.

Idempotent: cancelling a terminal voyage is a 200 with the current
status and no state change.

## Idempotency

`POST /voyages` canonicalizes the request (round times to the
minute, strip `max_candidates`, alphabetize fields) and hashes it.
If a voyage with the same `inputs_hash` exists:

- **`done` within cache TTL** → return `303 See Other` to its URL.
- **`queued` or live stage** → return `202` with that voyage's id
  (dedupe).
- **`failed` or `cancelled`** → create a fresh voyage with new id.
- Explicit `?force=true` always creates a new job.

## API surface

See doc 10 for full contract. Shape:

- `POST /voyages` → `202 Accepted`, returns voyage in
  `status="queued"` with polling links.
- `GET /voyages/{id}` → status + progress + (when `done`) the full
  voyage document.
- `GET /voyages/{id}/events` → SSE stream of status / progress
  (post-MVP nicety; polling covers MVP).
- `GET /voyages/{id}/gpx` → `404 VOYAGE_NOT_READY` until `done`.
- `GET /voyages/{id}/trace` → `PlanTrace` — populated progressively.
- `POST /voyages/{id}/cancel`.

## Observability

One root span `voyage.job` per job, child spans per stage:
`job.charts_fetching`, `job.charts_preprocessing`,
`job.forecast_prefetching`, `job.routing`, `job.scoring`,
`job.finalizing`. Each wraps the real work spans (see doc 14 trace
topology).

Metrics:

- `bv.jobs.submitted` counter
- `bv.jobs.completed{status}` counter (`done` / `failed` /
  `cancelled`)
- `bv.jobs.duration_seconds{status}` histogram
- `bv.jobs.queue_depth` gauge
- `bv.jobs.stage_duration_seconds{stage}` histogram
- `bv.jobs.stage_transitions{from,to}` counter

Logs on every status transition:
`event=job.status stage=routing pct=0.48 voyage_id=...`

## Testing

- Unit: state transitions; progress throttling; per-bbox lock
  dedupe behavior; crash-recovery sweep.
- Integration: full job lifecycle in-process with fake charts and
  fake forecasts (no network).
- Crash recovery: simulate mid-routing restart; assert the row ends
  `failed` with `WORKER_RESTARTED`.
- Cancellation: submit, wait for `routing`, cancel, assert
  `cancelled` + partial caches retained.

## Notes

- **Persisting per-stage progress history** (for a "where did the
  time go?" UI): keep only the latest blob in MVP; timeline comes
  from OTel traces.
- **Priority queue** is not MVP, single-user local tool.
