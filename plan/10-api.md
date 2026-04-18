# 10 — API

**Status:** draft

Small surface. REST-ish. JSON in, JSON or GPX out. OpenAPI is generated
by FastAPI from the Pydantic schemas in `app/schemas/`.

**Voyage planning is asynchronous.** `POST /voyages` accepts a job and
returns `202 Accepted`; the client polls `GET /voyages/{id}` (or
subscribes to `/events`) for progress. Chart fetches and isochrone runs
take minutes, not milliseconds — there is no synchronous plan mode.
See doc 15 for the job model.

## Endpoints

### `POST /voyages`

Submit a voyage plan.

**Request**

```json
{
  "origin":      { "lat": 38.9784, "lon": -76.4922, "name": "Annapolis" },
  "destination": { "lat": 36.8467, "lon": -76.2929, "name": "Norfolk" },
  "window": {
    "start_at": "2026-04-20T00:00:00Z",
    "end_at":   "2026-04-27T00:00:00Z",
    "tz":       "America/New_York",
    "earliest_departure_local_time": "06:00",
    "latest_departure_local_time":   "18:00"
  },
  "boat_profile_name": "saltbreaker",
  "objective":         "fastest",
  "max_candidates":    5
}
```

- `boat_profile_name` references a saved `BoatProfile` (see
  `/boat_profiles`). The profile is copied into `bv:request` on the
  completed voyage for reproducibility.
- `objective` ∈ `"fastest" | "comfortable" | "short_tacks"`; default
  `"fastest"`. See doc 04.

**Response — 202 Accepted**

```json
{
  "id":         "vy_01HXYZ...",
  "status":     "queued",
  "created_at": "2026-04-17T12:00:00Z",
  "progress":   { "stage": "queued", "pct": 0 },
  "links": {
    "self":   "/voyages/vy_01HXYZ...",
    "events": "/voyages/vy_01HXYZ.../events",
    "gpx":    "/voyages/vy_01HXYZ.../gpx",
    "trace":  "/voyages/vy_01HXYZ.../trace",
    "cancel": "/voyages/vy_01HXYZ.../cancel"
  }
}
```

### `GET /voyages/{id}`

Return current voyage state.

While running:

```json
{
  "id":        "vy_01HXYZ...",
  "status":    "routing",
  "progress": {
    "stage":  "routing",
    "pct":    0.48,
    "detail": "80 / 168 candidates routed",
    "eta_s":  120
  },
  "voyage":    null,
  "error":     null
}
```

On completion (`status="done"`):

```json
{
  "id":     "vy_01HXYZ...",
  "status": "done",
  "voyage": { /* full GPX-mirrored document — see below */ },
  "error":  null
}
```

On failure (`status="failed"`):

```json
{
  "id":     "vy_01HXYZ...",
  "status": "failed",
  "error":  {
    "code":   "CHARTS_NOT_AVAILABLE",
    "detail": "Bbox has gaps ENC/OSM can't cover: [[-75.5, 36.5, -74.5, 37.5]]",
    "stage":  "charts_fetching"
  }
}
```

Status values: `queued` / `charts_fetching` / `charts_preprocessing`
/ `forecast_prefetching` / `routing` / `scoring` / `finalizing` /
`done` / `failed` / `cancelling` / `cancelled` (see doc 15).

### `GET /voyages/{id}/events`

Server-Sent Events stream of status transitions and progress updates.
Optional for MVP — polling covers it.

```
event: progress
data: {"stage":"routing","pct":0.48,"detail":"80/168 candidates"}

event: status
data: {"status":"done"}
```

### `GET /voyages/{id}/gpx`

Return the GPX file. `404 VOYAGE_NOT_READY` until `status="done"`.

Query params:

- `candidate=<rank>` — single candidate + its contingencies; omit for
  the master file.

Response: `200 OK`,
`Content-Type: application/gpx+xml`,
`Content-Disposition: attachment; filename="voyage-{id}.gpx"`.

### `GET /voyages/{id}/trace`

Return the `PlanTrace` JSON (doc 14). Works at any status —
populated progressively during routing; on `failed` jobs it's the
primary diagnostic (last isochrone reached, per-candidate outcomes).

### `POST /voyages/{id}/cancel`

Cancel a running job. Idempotent; cancelling a terminal voyage is a
200 with the current status and no state change.

**Response — 200 OK**

```json
{ "id": "vy_01HXYZ...", "status": "cancelling" }
```

The row transitions to `cancelled` once the task observes the signal
(usually < 1 s).

### `GET /health`

Liveness. `{"status": "ok", "version": "..."}`.

### `GET /pois`

Return POIs in a bounding box. Convenience / debugging.

Query params:

- `bbox=minLon,minLat,maxLon,maxLat` (required)
- `sym=Anchor,Marina` (optional, comma-separated)
- `type=anchorage,hazard` (optional, comma-separated)

Response: array of `Waypoint` objects (see below).

### Boat profiles

- `GET /boat_profiles` — list names + summaries.
- `GET /boat_profiles/{name}` — full profile.
- `PUT /boat_profiles/{name}` — create or replace (see doc 01 fields).
- `DELETE /boat_profiles/{name}` — delete.

`PUT` body:

```json
{
  "name":               "saltbreaker",
  "polar_path":         "app/data/polars/cruiser_40ft_moderate.pol",
  "draft_m":            1.8,
  "beam_m":             3.8,
  "max_wind_kts":       30,
  "max_seas_m":         2.5,
  "min_depth_m":        0.5,
  "night_sailing_ok":   true,
  "motor_available":    true,
  "motor_min_wind_kts": 5
}
```

## Idempotency & single-voyage retention

The system stores **at most one voyage** at a time (doc 11). Every
`POST /voyages` either reuses that row or replaces it — dedupe and
retention are one flow, not two.

`POST /voyages` canonicalizes the request (round times to the minute,
strip `max_candidates`, canonicalize waypoints) and hashes it
(`inputs_hash`). Then, against the (possibly empty) existing row:

| Existing voyage                                 | Response |
|-------------------------------------------------|----------|
| none                                            | `202` — create, run |
| same `inputs_hash`, `done` within forecast TTL  | `303 See Other` → its URL (no writes) |
| same `inputs_hash`, `queued` / live             | `202` with its id (dedupe) |
| same `inputs_hash`, terminal beyond TTL         | `202` — replace (delete + create) |
| different `inputs_hash`, terminal               | `202` — replace (delete + create) |
| different `inputs_hash`, `queued` / live        | `409 VOYAGE_IN_PROGRESS` |

`?force=true` always replaces: any live voyage is cancelled, its task
is awaited, the row is deleted, a fresh voyage is created.

## `voyage` shape (when `status="done"`)

GPX-native (doc 01). `metadata`, `waypoints[]`, `routes[]`, plus
`extensions.bv` with request / coverage / inputs hash.

```json
{
  "metadata":  { "name": "Annapolis → Norfolk", "bounds": { /* ... */ } },
  "waypoints": [ /* origin, destination, navaids */ ],
  "routes":    [ /* primary candidates + contingencies */ ],
  "extensions": {
    "bv": {
      "inputsHash": "sha256:...",
      "request":    { /* normalized VoyageRequest */ },
      "coverage": {
        "forecast": "open-meteo-marine",
        "tides":    "noaa",
        "currents": "open-meteo-marine",
        "charts": {
          "enc_cells":            12,
          "osm_extracts":         0,
          "gebco_tile":           "gebco_2024_sub_ice_topo",
          "fetched_at":           "2026-04-17T12:00:00Z",
          "tide_modulated_depth": false
        },
        "stale_at":                     null,
        "forecast_horizon_exceeded_at": null
      }
    }
  },
  "skipped": {
    "route_blocked":        0,
    "route_timeout":        2,
    "route_limit_exceeded": 5,
    "route_no_coverage":    0
  }
}
```

## `Route` shape

Primary and contingency routes share the shape (doc 06 for contingency
extensions):

```json
{
  "name":   "Candidate 1",
  "type":   "primary",
  "rtepts": [ /* see rtept below */ ],
  "extensions": {
    "bv": {
      "rank":     1,
      "departAt": "2026-04-21T10:00:00Z",
      "arriveAt": "2026-04-22T08:30:00Z",
      "score": {
        "total":      82.4,
        "components": {
          "wind": 0.82, "waves": 0.91, "swell": 0.95,
          "current": 0.74, "tide": 1.0, "comfort": 0.78, "smg": 0.88
        }
      },
      "summaryMd":         "Tuesday-morning departure, beam reach most of the first day...",
      "contingencyKind":   null,
      "trigger":           null,
      "backupDestinations": [
        { "name": "Hampton", "lat": 37.0, "lon": -76.35, "detour_nm": 3.1 }
      ]
    }
  }
}
```

Contingency routes add to `extensions.bv`:

```json
"contingencyKind": "escape_hatch_route",
"trigger":         { "seasMGt": 2.0, "windKtsGt": 25 },
"parentRtept":     "DP-3"
```

## `rtept` shape

Mirrors GPX `<rtept>` with `bv:` planning extensions:

```json
{
  "lat":  38.5,
  "lon": -76.3,
  "name": "WP-03",
  "desc": "2026-04-21T14:30 — beam reach, 14kt @ 205, 0.8m seas",
  "sym":  "Waypoint",
  "extensions": {
    "bv": {
      "plannedAt":  "2026-04-21T14:30:00Z",
      "bearingDeg": 182.4,
      "distanceNm": 12.6,
      "maneuver":   "tack",
      "env":        { /* see doc 05 */ },
      "legScore":   { /* see doc 05 */ },
      "tapOut":     [ /* see doc 06 */ ]
    }
  }
}
```

## Errors

Standard FastAPI body + correlation id:

```json
{ "detail": "VOYAGE_NOT_READY", "correlation_id": "..." }
```

HTTP-level (validation / request-level routing):

| Code                     | Status | Meaning                                                          |
| ------------------------ | ------ | ---------------------------------------------------------------- |
| `INVALID_WINDOW`         | 400    | `start_at ≥ end_at`, or window > 14 days.                        |
| `INVALID_BOAT`           | 400    | Missing polar file, implausible draft/beam.                      |
| `BOAT_PROFILE_NOT_FOUND` | 404    | `boat_profile_name` has no saved profile.                        |
| `VOYAGE_NOT_FOUND`       | 404    |                                                                  |
| `VOYAGE_NOT_READY`       | 404    | GPX requested before `status="done"`.                            |
| `VOYAGE_IN_PROGRESS`     | 409    | Different-input voyage is live; use `?force=true` to cancel + replace. |
| (validation)             | 422    | FastAPI Pydantic failures.                                       |
| `UPSTREAM_UNAVAILABLE`   | 503    | Sync endpoints (e.g., boat profiles, /pois) when DB unreachable. |

Job-level failure codes (inside `voyage.error.code` when
`status="failed"`):

| Code                   | Stage                | Meaning                                                              |
| ---------------------- | -------------------- | -------------------------------------------------------------------- |
| `CHARTS_NOT_AVAILABLE` | charts_fetching      | Bbox has gaps ENC ∪ OSM can't cover (or GEBCO unavailable).          |
| `CHARTS_FETCH_FAILED`  | charts_fetching      | Network / upstream error during the fetch. Retryable.                |
| `CHARTS_STALE`         | charts_fetching      | Cached cells older than `BV_CHARTS_MAX_AGE_DAYS` and refresh failed. |
| `FORECAST_UNAVAILABLE` | forecast_prefetching | Open-Meteo / NOAA unreachable and no cache covers the window.        |
| `ROUTE_BLOCKED`        | routing              | No feasible route from origin under any departure.                   |
| `WORKER_RESTARTED`     | any                  | Process restarted mid-job; retry.                                    |

Per-candidate failure reasons inside `voyage.skipped`:
`route_blocked`, `route_timeout`, `route_limit_exceeded`,
`route_no_coverage`. A voyage succeeds if any candidate survives.

## Versioning

Prefix-less for MVP. `/v1/` when we need it.

## Auth

None in MVP. Per-user voyage history is a post-MVP story.

## Notes

- **Polling cadence hint** via `Retry-After` on `202` responses.
  Easy add.
- SSE  for `/events` 
- 
