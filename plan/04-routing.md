# 04 — Routing (weather / isochrone)

**Status:** draft

## What weather routing is

Given `(origin, destination, depart_at, boat_polar, forecast_field)` a
weather router solves for the route that optimizes an objective function
(usually minimum ETA) under the forecast wind/wave/current field, using
the boat's actual performance curve. The standard algorithm is
**isochrones**: at each time step, compute the set of positions
reachable from the previous isochrone, prune it, and advance until the
destination is in reach. Avalon Offshore, QtVlm, PredictWind, and Squid
all do variants of this.

## Inputs

- **Geometry:** `origin: LatLon`, `destination: LatLon`, `depart_at: datetime` (UTC).
- **Boat:** `BoatProfile` — polars (CSV), hard limits (`max_wind_kts`,
  `max_seas_m`, `min_depth_m`), preferences (`night_sailing_ok`,
  `motor_available`, `motor_min_wind_kts`).
- **Forecast field:** `ForecastField` — callable `(lat, lon, t) → Env`
  with `wind_speed, wind_dir, wave_height, wave_period, current_speed,
  current_dir`. Backed by cached Open-Meteo Marine grids; bilinear
  spatial + linear temporal interpolation.
- **Chart store:** `ChartStore` — land, obstacles, restricted areas,
  bathymetry, and navaids. See **doc 03** for data sources (NOAA ENC,
  GEBCO, OpenSeaMap, `pois.gpx`) and the preprocessed layer model.
  The router treats `ChartStore` as an opaque spatial service.
- **Objective:** enum — `fastest` (default), `comfortable`,
  `short_tacks` (see *Objective function* below).

## Boat polars

- Standard CSV format: first row `TWA\TWS, 4, 6, 8, 10, 12, 16, 20, ...`;
  subsequent rows `0, 45, 52, 60, ...`; cells are boat speed in knots.
- Same format OpenCPN and QtVlm import — round-trippable.
- `BoatProfile.polar_path: Path` points to a file. Users bring their own.
- We ship a library under `app/data/polars/` with nominal cruisers
  (`cruiser_30ft_moderate.pol`, `cruiser_40ft_performance.pol`, etc.)
  sourced from public ORC data and clearly labelled as nominal.
- Interpolation: bilinear in `(TWA, TWS)` space. `TWS` beyond the
  polar's max column → extrapolate flat (no extra speed from more wind).

## Forecast field

- **Wind + waves + ocean current** all come from Open-Meteo Marine
  (`ocean_current_velocity`, `ocean_current_direction` are part of the
  marine API at hourly ~0.1° resolution).
- **Tides** (NOAA) remain station-based and only consulted for
  shallow-waypoint checks — not for routing.
- `ForecastField.at(lat, lon, t) → Env` does:
  - Locate the surrounding 4 grid cells + 2 time slices.
  - Bilinear in space, linear in time.
  - Cache the last accessed cell per coroutine for locality.
- **Gaps:** any interpolation over missing data returns `None`
  explicitly — no silent zero-fill. Router treats `None` as
  `ROUTE_NO_COVERAGE` at that point.

## Core loop (isochrone)

```
iso[0]  = [IsochronePoint(origin, t=depart_at, parent=None, heading=None)]
trails  = []
for step in 1..max_steps:
    t = depart_at + step * dt
    frontier = []
    for pt in iso[step-1]:
        env = forecast.at(pt.lat, pt.lon, t)
        if env is None:                    # outside coverage
            continue
        for h in heading_fan(step, pt):
            twa = relative_angle(h, env.wind_dir)
            bsp = polar(|twa|, env.wind_speed)
            if bsp is None: continue               # in-irons / motor
            if violates_wind_wave_limits(env, boat):
                continue
            dxdy = heading_vector(h) * bsp * dt + env.current_vec * dt
            new  = advance_geodesic(pt, dxdy)
            if charts.crosses_land(pt, new):            continue
            if charts.crosses_obstacle(pt, new):        continue
            if charts.is_restricted(new):               continue
            depth = charts.available_depth(new.lat, new.lon, t)
            if depth is None:                           continue   # uncovered
            if depth < boat.draft_m + boat.min_depth_m: continue   # too shallow
            frontier.append(IsochronePoint(new, t, parent=pt, heading=h))
    iso.append(prune(frontier, destination, step))
    if reached(iso[step], destination, tol=arrival_tolerance_nm):
        return backtrack(iso, destination)
raise RouteTimeout(best_partial=iso[-1])
```

## Heading fan

- Default: every 10° from 0–359° → 36 directions.
- Tighter fan (every 3°) within ±20° of bearing-to-destination — cheap
  resolution where it matters.
- Total ≈ 50 headings per point.

## Pruning

**MVP: sector pruning** (Hagiwara 1989 style).

1. Axis = great-circle bearing from *this isochrone's centroid* toward
   destination.
2. Divide ±90° around that axis into 72 sectors (2.5° each).
3. For each sector, keep exactly one point — the one that maximizes
   progress toward destination.
4. Drop sectors pointing "behind."

Result: the pruned isochrone has ≤72 points regardless of frontier
size, which caps the per-step cost.

**Pareto pruning** (for multi-objective like time + comfort) is a
post-MVP upgrade; the code path is designed to swap prune() without
touching the rest.

## Termination

- **Success:** any point on the latest isochrone is within
  `arrival_tolerance_nm` of the destination. Default 0.3 nm.
- **Timeout:** step count exceeds `max_passage_hours / dt`. Default
  `max_passage_hours = 168` (7 days — matches forecast horizon).
- **Unroutable origin:** first isochrone is empty → `ROUTE_BLOCKED`.
- **Loss of coverage:** forecast interpolation returns `None` for every
  frontier point at some step → `ROUTE_NO_COVERAGE`.

Any failure still produces a `PlanTrace` (doc 14) including the last
reached isochrone, for diagnosability.

## Backtracking

Walk `parent` pointers from the winning terminal point back to origin.
Result: chronologically ordered list of `IsochronePoint` with
(lat, lon, time, heading, env) on each.

## Objective function

The router optimizes one objective per run. This is distinct from the
per-candidate `Score` (doc 05), which is a post-hoc summary.

| Objective     | Minimizes                                        |
| ------------- | ------------------------------------------------ |
| `fastest`     | ETA                                              |
| `comfortable` | ETA + α·Σ(wave_height²·dt) + β·Σ(wind_kts>20)·dt |
| `short_tacks` | ETA + γ·(number of maneuvers)                    |

Selected in the request body (see doc 10). Weights are tunable
constants, not per-request knobs.

## Decimation (isochrone → GPX)

A raw backtracked route has hundreds to thousands of points. OpenCPN
routes should be tens. We emit only:

1. **Origin and destination**, always.
2. **Maneuver points:** any point where heading changes by
   `maneuver_threshold` (default 15°). These become
   `bv:maneuver = "tack" | "gybe"` markers.
3. **Hourly time markers** for operator situational awareness.
4. **Decision points:** every 4 hours of elapsed time OR when the
   scored environment crosses a threshold — these are the anchor points
   for contingencies (doc 06).
5. Between preserved points, **Douglas-Peucker** simplification with
   `simplify_tolerance_nm` (default 0.1 nm) removes intermediate
   geometry.

Target: 20–100 rtepts per emitted route.

## What a "leg" is now

A leg is a pair of adjacent rtepts in the emitted route. Each rtept
carries `bv:env`, `bv:plannedAt`, `bv:bearingDeg`, `bv:distanceNm` (from
the previous rtept), and `bv:legScore`. Legs correspond to **sailing
between maneuvers**, not straight-line passages between static
waypoints. The domain model (doc 01) is unchanged — the semantics of
"leg" are just sharpened.

## Chart integration

Land, obstacles, restricted areas, and bathymetry all come from a
`ChartStore` owned by `services/charts.py`. Sources are NOAA ENC,
OpenSeaMap, and GEBCO; all three are **required** (no degraded
fallback). Acquisition happens inside the voyage job's
`charts_fetching` stage. Full data-source story, coverage policy,
preprocessing, and failure modes live in **doc 03**.

Four queries per propagated motion (see the core loop above):

1. `charts.crosses_land(a, b)` — NOAA ENC (`LNDARE` + `COALNE`) where
   US, OpenSeaMap elsewhere.
2. `charts.crosses_obstacle(a, b)` — wrecks / rocks / obstructions
   from ENC + OSM, plus user-declared hazards from `pois.gpx`
   unioned into the same layer.
3. `charts.is_restricted(pt)` — `RESARE` / `CTNARE` / `MARCUL` from
   ENC, OSM `seamark:type=restricted_area`.
4. `charts.available_depth(lat, lon, t)` vs
   `boat.draft_m + boat.min_depth_m` — ENC `DEPARE` where available
   else GEBCO, plus optional tide offset.

If `ChartStore.coverage(bbox)` returns any gap, the voyage job
terminates with `CHARTS_NOT_AVAILABLE` (doc 10) — never falls back to
synthetic coastlines or "assume deep water everywhere."

## Forecast-horizon handling

- Open-Meteo's marine horizon is ~7 days hourly.
- If routing requires forecast beyond the horizon:
  - Use the last available hour's field for extrapolated time (no
    extrapolation of weather).
  - Flag the voyage with
    `bv:coverage.forecastHorizonExceededAt = <datetime>`.
  - NL summary calls this out prominently ("the last 12 hours of this
    passage are beyond the forecast horizon — expect this plan to
    change").

## Complexity & performance

- `dt = 30 min`, 72-hour passage → 144 steps.
- 72 pruned points × 50 headings = 3,600 propagations per step.
- × 144 steps = **~520k propagations per candidate**.
- Polar lookup, bilinear interpolation, geodesic step, segment test are
  all vectorizable with numpy. Target budget: 1–2 s per candidate on a
  laptop.
- Candidate departure times are embarrassingly parallel — fan out via
  `asyncio.gather` bounded by CPU count (each candidate runs sync
  numpy inside an executor).

## Implementation

New module: `app/services/router.py` houses the isochrone kernel as
pure numpy. Thin async wrapper in `app/services/planner.py` calls it
per candidate.

Dependencies to add to `pyproject.toml` at M2:

- `numpy>=2.0`
- `scipy>=1.14` (bilinear grid interpolation)
- `pyproj>=3.7` (geodesic step + bearing)
- `shapely>=2.0` (STRtree spatial indices)
- `netCDF4>=1.7` + `xarray>=2024.10` (GEBCO bathymetry)
- `pyogrio>=0.9` (ENC / vector chart ingest — see doc 03)

## Observability

Router emits (see doc 14):

- Span `router.plan_candidate` with attributes `dt_s`,
  `n_steps_executed`, `n_frontier_points_total`, `arrival_dist_nm`,
  `outcome`.
- Child span `router.isochrone_step` per time step (sampled if step
  count > 200, to cap trace volume).
- Metrics:
  - `bv.router.steps` histogram
  - `bv.router.propagations_per_step` histogram
  - `bv.router.outcomes` counter (`ok` / `timeout` / `no_coverage` / `blocked`)
  - `bv.router.wallclock_seconds` histogram

The `PlanTrace` persists every isochrone layer (decimated) so we can
replay a failed route after the fact.

## Validation

- **Golden-route tests:** pin (`start`, `end`, `depart_at`, `polar`,
  `forecast fixture`) → expected decimated waypoints within tolerance.
  Any algorithm change that moves a golden route requires a human
  review + fixture update.
- **Cross-reference:** a small `tests/benchmarks/` set of passages run
  through QtVlm manually; our ETA must match within 10% and our route
  should be "visually similar" (Fréchet distance TBD).
- **Sanity checks:**
  - Zero-distance passage → empty route, not an error.
  - Routing into a closed harbor → `ROUTE_BLOCKED` within one step.
  - No-wind forecast + no motor → `ROUTE_TIMEOUT`.

## Failure modes

| Code                   | Meaning                                                    |
| ---------------------- | ---------------------------------------------------------- |
| `ROUTE_BLOCKED`        | First isochrone is empty (obstacles everywhere).           |
| `ROUTE_NO_COVERAGE`    | Forecast field has gaps covering the propagation frontier. |
| `ROUTE_TIMEOUT`        | Destination unreachable within `max_passage_hours`.        |
| `ROUTE_LIMIT_EXCEEDED` | Every frontier point violates a hard limit.                |

All surface as structured planner errors; the `PlanTrace` stores the
last isochrone reached.

## Notes

- **Heel / leeway:**  trust the polar.
- **Motor-sailing:** treat as an extra polar column for `TWS < 5 kt`
  with a speed penalty and a fuel-burn attribute. Ship nominal.
- **Time step:** 30 min default.  adaptive — tighten near start and
  near destination.
- **Pareto routing:**  if benchmarking shows single-objective produces too many "fast but miserable" options, add this.
- **Chart data:**  fully owned by doc 03.
