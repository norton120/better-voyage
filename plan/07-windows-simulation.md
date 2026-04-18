# 07 — Departure windows & ranking

**Status:** draft

This doc covers **how we enumerate candidate departures** and produce
a ranked list.  Each candidate is one isochrone router run (doc 04). This doc is about the wrapper around that.

## Enumeration

Given a `TimeWindow(start_at, end_at)` plus optional local-time
constraints:

1. Generate candidate departure times on a grid. Default granularity
   is every **1 hour** between `start_at` and `end_at`.
2. Apply local-time constraints (`earliest_departure_local_time`,
   `latest_departure_local_time`) — drop candidates outside.
3. If `boat.night_sailing_ok = False`, drop candidates whose *arrival*
   would fall in local 22:00–06:00 (arrive in daylight > depart in
   daylight).

**Volume control:** a 7-day window at hourly granularity is 168
candidates. We route all of them but return only the top
`max_candidates` (default 5) to the user. The rest stay in the DB and
are queryable.

## Prefetch (once per voyage)

Before routing, the planner populates the forecast-field cache:

1. Compute the bounding box between origin and destination, padded by
   `PREFETCH_MARGIN_NM` (default 50 nm, to cover weather-routed
   detours).
2. Time range = full `TimeWindow` + `max_passage_hours` on the
   trailing edge.
3. Batch-fetch Open-Meteo Marine grids for that bbox × time range
   (wind, waves, swell, ocean current).
4. Batch-fetch NOAA tide predictions for any shallow waypoint within
   the bbox.
5. Persist to SQLite with normal TTL (doc 11).

After prefetch, `ForecastField.at(lat, lon, t)` is a pure in-memory /
SQLite lookup. No network calls during routing.

## Parallel routing

Each candidate is one isochrone run (doc 04) — fully independent. They
run concurrently under a bounded semaphore; the CPU-intensive numpy
kernel runs in a thread executor so the event loop isn't starved.

Default concurrency: `BV_MAX_CONCURRENT_ROUTES` = CPU count.

## Ranking

1. Discard candidates with router failures (`ROUTE_TIMEOUT`,
   `ROUTE_BLOCKED`, `ROUTE_LIMIT_EXCEEDED`, `ROUTE_NO_COVERAGE`).
   Their counts surface in `skipped` per-failure-reason.
2. Score the survivors (doc 05).
3. Sort by `score.total` desc, break ties by `depart_at` asc
   (stable sort).
4. Take the top `max_candidates`.

## Per-candidate post-processing (top-N only)

For each surfaced candidate:

1. Derive contingencies (doc 06) — may trigger additional isochrone
   runs.
2. Render NL summary (doc 08).
3. Assemble into the voyage's `routes[]`.

We don't post-process non-surfaced candidates; they're still
retrievable from the PlanTrace but don't get contingencies or
summaries.

## Offline behavior

If the cache misses for any hour the router needs **and** the network
is unreachable:

- The specific candidate returns `ROUTE_NO_COVERAGE` and is dropped.
- The voyage response surfaces `skipped.offline_coverage = N` and
  `coverage.stale_at`.
- If **all** candidates skip for coverage, the voyage response is
  `503 UPSTREAM_UNAVAILABLE` (see doc 10).

## Determinism

Same request + same cached dataset ⇒ same top-N in the same order.
Sort is stable; all downstream functions are pure.

## Pseudocode

```python
async def plan(req: VoyageRequest) -> Voyage:
    forecast  = await prefetch_forecast_field(req, pad_nm=PREFETCH_MARGIN_NM)
    obstacles = build_obstacle_index(pois, coastline)
    boat      = load_boat_profile(req.boat_profile_name)

    departures = enumerate_departures(req.window, boat)

    async with bounded_semaphore(settings.max_concurrent_routes):
        routed = await asyncio.gather(*[
            run_isochrone_in_executor(
                start=req.origin, end=req.destination, depart_at=t,
                boat=boat, forecast=forecast, obstacles=obstacles,
                objective=req.objective,
            )
            for t in departures
        ])

    candidates = [score_candidate(r) for r in routed if r.ok]
    ranked     = sorted(candidates, key=lambda c: (-c.score.total, c.depart_at))
    top        = ranked[: req.max_candidates]

    for c in top:
        c.contingencies = derive_contingencies(c.route, pois, forecast, boat)
        c.summary_md    = render_summary(c)

    return assemble_voyage(req, top, skipped_counts(routed))
```

## Observability

- Root span `voyage.plan` with `bv.candidates.enumerated`,
  `bv.candidates.routed`, `bv.candidates.returned` attributes.
- Child spans: `prefetch`, `router.plan_candidate` × N, `score`,
  `derive_contingencies`, `render_summary`, `persist`, `emit_gpx`.
- Metrics: `bv.voyages.planning_duration_seconds`,
  `bv.voyages.candidates_total`, `bv.voyages.candidates_rejected{reason}`.

## Open questions

- Hourly departure grid is fine.
- Retention of PlanTraces for non-surfaced candidates: TTL-prune after routes are selected.
