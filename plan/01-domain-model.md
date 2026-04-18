# 01 — Domain model

**Status:** draft

## Principle: GPX-native

The domain model mirrors GPX 1.1 directly. Our Pydantic models **are** GPX
elements, and our planning data lives in `<extensions>` under a `bv:`
namespace.

This makes ingest lossless (any GPX from OpenCPN, OpenPlotter, Squid,
Active Captain exports, etc. round-trips through us untouched) and
emission trivial (serialize the in-memory tree; no translation layer).

Namespace: `xmlns:bv="https://better-voyage.app/gpx/1"`

## Core GPX entities

### `Waypoint` (≡ `<wpt>` / `<rtept>`)

1:1 with GPX 1.1. Required fields in **bold**.

- **`lat: float`**, **`lon: float`** (WGS84)
- `ele: float | None` (meters)
- `time: datetime | None` (UTC)
- `name: str | None`
- `cmt: str | None` (short comment)
- `desc: str | None` (longer description)
- `src: str | None` (provenance — e.g. `"user"`, `"noaa_station"`,
  `"seed"`, `"ac_import"`)
- `link: list[Link]` (URLs with optional `text`, `type`)
- `sym: str | None` (icon name — `"Anchor"`, `"Marina"`, `"Waypoint"`)
- `type: str | None` (free-form classification — e.g. `"anchorage"`,
  `"hazard"`)
- `extensions: Extensions`

`sym` and `type` are the categorization,
same as every other GPX consumer.

### `Route` (≡ `<rte>`)

- `name, cmt, desc, src: str | None`
- `link: list[Link]`
- `number: int | None`
- `type: str | None` (e.g. `"primary"`, `"contingency"`)
- `rtepts: list[Waypoint]`
- `extensions: Extensions`

### `Track` (≡ `<trk>`)

Not emitted by the planner (tracks are for recorded history). Declared
so the parser handles inbound GPX containing `<trk>` without erroring.

### `Voyage` (≡ a GPX document)

- `version: "1.1"`
- `creator: "better-voyage/<semver>"`
- `metadata: Metadata` (`name, desc, time, author, link, bounds`)
- `waypoints: list[Waypoint]` (top-level `<wpt>` — origin, destination,
  and any POIs referenced by routes)
- `routes: list[Route]` (primary candidates + contingencies)
- `tracks: list[Track]` (empty on emission; populated on ingest)
- `extensions: Extensions`

## The `bv:` extensions

`Extensions` is a typed Pydantic model that serializes to/from
`<extensions>` XML under `bv:`. Adding a field is a schema change.
Unknown `bv:*` (or foreign-namespace) elements encountered on ingest
are preserved in a `raw: dict[str, Any]` bag so round-trip is lossless.

### On a `Waypoint` (POI metadata)

- `bv:shelterQuadrants: list["N","NE","E","SE","S","SW","W","NW"] | None`
- `bv:vhfChannel: int | None`
- `bv:amenities: dict[str, Any] | None` (open bag — fuel, water,
  pumpout, wifi, transient_slips, approach_depth_m, ...)
- `bv:depthM: float | None`
- `bv:notes: str | None` (separate from GPX `desc`/`cmt`, which
  round-trip to other consumers)

### On a route's `rtept` (per-leg planning output)

A "leg" is a pair of adjacent `rtepts`. Planning data attaches to the
**terminating** rtept of each leg.

- `bv:plannedAt: datetime` (UTC arrival)
- `bv:bearingDeg: float` (initial bearing from the previous rtept)
- `bv:distanceNm: float`
- `bv:env: LegEnvironment`
- `bv:legScore: Score`

### On a `Route` (candidate metadata)

- `bv:rank: int` (1 = highest-scoring)
- `bv:score: Score`
- `bv:departAt: datetime`, `bv:arriveAt: datetime`
- `bv:summaryMd: str` (NL pros/cons — see doc 08)
- `bv:contingencyKind: "backup_anchorage" | "tap_out_marina" | "escape_hatch_route" | None`
- `bv:trigger: Trigger | None` (only on contingency routes)

### On a `Voyage` (document-level)

- `bv:request: VoyageRequest` (normalized request)
- `bv:coverage: Coverage` (sources + staleness)
- `bv:inputsHash: str`

## Non-GPX value objects

These don't fit inside a GPX element — they live in API
requests/responses and as `bv:` extension payloads.

### `LegEnvironment`

Aggregated forecast/tide data for a leg.

- `windKtsAvg, windKtsMax, windDirDegAvg`
- `windAngleRelDeg` (relative to the leg's course)
- `waveHeightMAvg, waveHeightMMax`
- `swellPeriodS, swellDirDeg`
- `currentKtsAvg, currentDirDeg`
- `tideLowMMin` (minimum tide over the leg, or null)

### `Score`

- `total: float` (0–100, higher is better)
- `components: dict[str, float]` (named sub-scores + weights)

### `Trigger`

Declarative thresholds for contingencies.

- `windKtsGt, seasMGt, visibilityMLt: float | None`
- `notes: str | None`

### `BoatProfile`

Inputs only. Stored in a separate `boat_profiles` table (see doc 11) and
referenced by name from `VoyageRequest`; serialized verbatim into
`bv:request` on the output `Voyage`.

- `name: str`
- `polar_path: Path` — CSV polar (TWA × TWS → BSP). See doc 04.
- `draft_m: float` (for shallow-waypoint tide checks)
- `beam_m: float` (informational)
- `max_wind_kts: float`, `max_seas_m: float` — **hard limits**; the
  router refuses to propagate through envelopes that violate these.
  They are not scoring penalties.
- `min_depth_m: float` — minimum under-keel clearance.
- `night_sailing_ok: bool`
- `motor_available: bool`, `motor_min_wind_kts: float | None` — when
  true-wind-speed drops below `motor_min_wind_kts`, the router may use
  a fallback "motor" column in the polar (doc 04).

### `TimeWindow`

- `start_at: datetime`, `end_at: datetime` (UTC)
- `tz: str` (IANA — for display only)
- `earliest_departure_local_time: time | None`
- `latest_departure_local_time: time | None`

### `VoyageRequest`

- `origin: Coord`, `destination: Coord`
- `window: TimeWindow`
- `boat_profile_name: str` (references a saved `BoatProfile`)
- `objective: "fastest" | "comfortable" | "short_tacks"` (default
  `fastest` — see doc 04 on router objectives)
- `max_candidates: int` (default 5)

## Relationships

```
Voyage (GPX doc)
  ├─ metadata
  ├─ waypoints[]           ← POIs, origin, destination
  └─ routes[]              ← candidates + contingencies
        └─ rtepts[]        ← each carries planned env + leg score
```

## Identity & idempotency

- `bv:inputsHash` on the voyage is a sha256 over the canonicalized
  `VoyageRequest`.
- Identical request within cache TTL returns the same voyage document
  (see doc 11).

## Why this works

- **No translation layer.** `gpxpy` ingests and emits directly; our
  Pydantic models wrap the same tree.
- **Lossless third-party GPX.** `sym`, `desc`, `cmt`, `link`, and
  foreign-namespace extensions survive ingest → enrich → emit.
- **OpenCPN / OpenPlotter compatible by construction.** We use their
  `<sym>` vocabulary as our own.
- **Active Captain exports, Squid outputs, hand-drawn OpenCPN routes**
  all deserialize into the same `Voyage` object.

## ## Non-GPX Components

- `BoatProfile` is a distinct table

# 
