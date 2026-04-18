# 11 — Storage & caching

**Status:** draft

Offline-friendliness is a requirement. Every upstream fetch is
persisted, every voyage is reproducible from cached inputs.

Because the domain is GPX-native (doc 01), **the voyage GPX blob is
the source of truth**. A thin index table carries hot query fields so
we don't parse the blob to list candidates. If the index is lost we
can rebuild it from the blobs.

## Database

- **SQLite** via `aiosqlite`.
- File: `BV_DATABASE_URL` (default `./data/better-voyage.db`,
  `/data/...` in Docker).
- Migrations: **Alembic**. 

## Tables

### `voyages`

Voyages are async jobs (doc 15). The row carries the full state
machine: request, current stage + progress, terminal outcome.

| column          | type         | notes                                                                                                                                                                  |
| --------------- | ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| id              | TEXT PK      | `vy_<ulid>`                                                                                                                                                            |
| created_at      | TIMESTAMP    | UTC, job submission time                                                                                                                                               |
| started_at      | TIMESTAMP    | when moved out of `queued`; null while queued                                                                                                                          |
| completed_at    | TIMESTAMP    | terminal-status time (`done` / `failed` / `cancelled`)                                                                                                                 |
| status          | TEXT INDEXED | `queued` / `charts_fetching` / `charts_preprocessing` / `forecast_prefetching` / `routing` / `scoring` / `finalizing` / `done` / `failed` / `cancelling` / `cancelled` |
| progress_json   | TEXT         | current stage blob: `{stage, pct, detail, eta_s}`                                                                                                                      |
| request_json    | TEXT         | canonicalized `VoyageRequest`                                                                                                                                          |
| inputs_hash     | TEXT INDEXED | sha256 of canonicalized request                                                                                                                                        |
| gpx_blob        | BLOB         | full master voyage GPX — null until `status="done"`                                                                                                                    |
| coverage_json   | TEXT         | sources used + staleness (populated by `finalizing`)                                                                                                                   |
| plan_trace_json | TEXT         | PlanTrace (doc 14), grows during routing                                                                                                                               |
| error_code      | TEXT         | on `failed` (see doc 10)                                                                                                                                               |
| error_detail    | TEXT         | on `failed`                                                                                                                                                            |
| error_stage     | TEXT         | stage at which failure occurred                                                                                                                                        |

Indexes:

- `(status, created_at)` — scheduler picks oldest `queued`.
- `(inputs_hash)` — idempotency lookup.

### `candidates_index`

Derived from `voyages.gpx_blob`; regeneratable. Exists purely to make
listing and sorting candidates cheap.

| column      | type              | notes |
| ----------- | ----------------- | ----- |
| voyage_id   | FK voyages.id     |       |
| rank        | INT               |       |
| depart_at   | TIMESTAMP         |       |
| arrive_at   | TIMESTAMP         |       |
| score       | REAL              |       |
| summary_md  | TEXT              |       |
| PRIMARY KEY | (voyage_id, rank) |       |

Full candidate detail is read from the GPX blob.

### `forecast_cache`

| column      | type      | notes                             |
| ----------- | --------- | --------------------------------- |
| key         | TEXT PK   | `open_meteo_marine:<params_hash>` |
| params_json | TEXT      | raw params for debugging          |
| body_json   | TEXT      | raw response body                 |
| fetched_at  | TIMESTAMP |                                   |
| expires_at  | TIMESTAMP |                                   |

### `tide_cache`

| column     | type      | notes                         |
| ---------- | --------- | ----------------------------- |
| key        | TEXT PK   | `noaa_tides:<station>:<date>` |
| station_id | TEXT      |                               |
| body_json  | TEXT      |                               |
| fetched_at | TIMESTAMP |                               |
| expires_at | TIMESTAMP |                               |

### `stations_cache`

| column     | type      | notes             |
| ---------- | --------- | ----------------- |
| id         | TEXT PK   |                   |
| kind       | TEXT      | `tide`, `current` |
| lat, lon   | REAL      |                   |
| name       | TEXT      |                   |
| payload    | TEXT      |                   |
| expires_at | TIMESTAMP |                   |

## POIs

POIs live in a GPX file — `app/data/pois.gpx` — committed to the repo
and loaded into memory at startup. A POI is just a `<wpt>` with `sym`,
`type`, and our `bv:` extensions (`shelterQuadrants`, `amenities`,
`vhfChannel`, ...). No separate POI table.

Benefits vs. a YAML seed:

- Edits round-trip through OpenCPN — draw waypoints on a chart, export
  GPX, commit.
- Drop-in supplements: users can point `BV_POI_DIRS` at a directory of
  extra GPX files (AC exports, OpenSeaMap extracts, hand-curated
  regions) and they merge at load time.
- No custom parser — same code path as any other GPX ingest.

Spatial queries use an in-memory R-tree built over the loaded
waypoints.

## Cache policy

- **Read-through.** Clients check cache first, fetch on miss, write
  back.
- **Stale-while-error.** On upstream failure, serve last cached body
  past TTL and surface `stale_at` in the voyage's `bv:coverage`.
- **TTL defaults** (from `app/config.py`):
  - forecast: 3 h
  - tide: 24 h
  - station metadata: 30 days

## Retention & idempotency (single-voyage model)

The `voyages` table holds **at most one row at any time**. Every
`POST /voyages` either reuses that row or replaces it. The two policies
— "dedupe identical submissions" and "only keep the latest voyage" —
are one coherent flow, not two.

Canonicalization:

- `POST /voyages` normalizes the request (round times to the minute,
  canonicalize waypoints, strip `max_candidates`) and hashes it into
  `inputs_hash`.

On submission, consult the (possibly empty) existing row:

| Existing voyage                                 | Action |
|-------------------------------------------------|--------|
| none                                            | insert, run |
| same `inputs_hash`, `done` within forecast TTL  | **reuse** — return `303` to its URL; no writes |
| same `inputs_hash`, `queued` / live             | **reuse** — return `202` with its id (dedupe) |
| same `inputs_hash`, terminal beyond TTL         | **replace** — delete row, insert fresh, run |
| different `inputs_hash`, terminal               | **replace** — delete row, insert fresh, run |
| different `inputs_hash`, `queued` / live        | `409 VOYAGE_IN_PROGRESS` (unless `?force=true`, which cancels + replaces) |

`?force=true` always replaces: if a live voyage is running it is
cancelled, the task's exit is awaited, the row is deleted, then the new
voyage is inserted and started.

Net effect: at any moment SQLite holds zero or one voyage row. There is
no voyage history to prune in a background sweep — retention is
enforced synchronously on submit.

## Cache pruning (independent of voyages)

- **Forecast, tide, station, and summary caches** are TTL-pruned by a
  background sweep each hour. They persist across voyage
  replacements — the value is in the cache hit, not the voyage row.
- **POIs** are immutable at runtime; reload on restart (or SIGHUP
  later).

## Concurrency

- SQLite WAL mode (`PRAGMA journal_mode=WAL`). Many readers, one writer
  — plenty for MVP.

## Open questions

- Prune raw upstream JSON indefinitely  after the
  voyage using it is deleted
