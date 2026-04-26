# 18 — Router perf investigation

**Status:** draft / handoff doc
**Owner:** unassigned (next session)
**Related:** [04-routing.md](./04-routing.md), [17-isochrone-overhaul.md](./17-isochrone-overhaul.md)

## TL;DR

Real-charts routing is **~7 s per isochrone step** — far slower than
the per-call cost of any individual chart query suggests. A
4-candidate Annapolis→St-Michaels run takes ~10 minutes; a
5-candidate Mathews→Solomons run hit the per-candidate wallclock cap
(900 s) on every candidate without reaching the destination. Solving
this is what stands between the app working and the app feeling
*usable*.

This doc captures everything the prior session learned so the next
session can skip the rediscovery and go straight to the profile +
optimization pass.

## Symptom

| Run | Mode | Distance | Wallclock | Steps/cand | s/step |
|---|---|---|---|---|---|
| Annapolis→St-Michaels (3 cands) | real | 16 nm | 601 s | ~95 | ~6 s |
| Annapolis→St-Michaels (7 cands, parallel) | real | 16 nm | 962 s | varied | similar |
| Mathews→Solomons (5 cands) | real | 43 nm | 5 × 900 s timeout | 510–547 | ~1.7 s |
| Open-water test (offshore origin) | real | ~50 nm | 189 s in 24 steps | 24 | **7.9 s** |
| Annapolis→Norfolk (5 cands) | null | 125 nm | 79 s | — | <0.5 s |

The null-mode numbers prove the routing kernel is fast. **All
slowness comes from real-mode chart queries** — but not in the way
you'd expect from a per-call profile.

## What we ruled out

These were measured directly, in-container, on the running real-mode
ChartStore for a Chesapeake bbox.

### `crosses_land` per call is fast

```
1000 unique segments through real chart data, margin_nm=0.1:
   192 ms total → 0.10 ms each
```

So the headline `crosses_land` cost is **100 µs**. Ten thousand calls
is one second. Even a maximally-busy step doing 6 000 spatial tests
× 0.1 ms = 600 ms. That accounts for ~10 % of the observed 7 s/step.
**There's another 6 seconds per step unaccounted for.**

### Prepared geometries don't help here

```
2000 unique segs WITH prep:    0.10 ms each
2000 unique segs WITHOUT prep: 0.08 ms each
```

The prepared-geom fast path was dropped on `e564487` because the
microbench showed it inside-noise for buffered-LineString-vs-Polygon
intersects. (Prep is a big win for `.contains()` against many points,
not so much for our use.) Don't waste time re-adding prep until you've
ruled out the *real* bottleneck below.

### The LineString-as-land bug is fixed

`e564487`. Was a separate correctness bug — not the perf cliff.

### Worker chart load is fast

```
load_existing_for_bbox: 2.0 s for 13 sources (7 041 land + 2 204 obstacle)
```

Per-worker chart load is not the cliff either.

### Voyage-level throughput

`_VOYAGE_WALLCLOCK_BUDGET_S = 7200` (2 h) and
`wallclock_budget_s = 900` (per-candidate, 15 min). Those are
generous; the issue is *individual* candidate latency, not budgets
being too tight.

## Hypotheses (ordered by my guess at likelihood)

### 1. Per-step overhead is dominated by something other than `crosses_land` calls

Each `plan_candidate` step does, per surviving frontier point × per
heading in the fan:

- forecast lookup (`forecast.at(lat, lon, t)`)
- relative wind angle
- polar BSP lookup
- geodesic advance via `pyproj.Geod.fwd` (likely)
- *current* drift advance (second `pyproj.Geod.fwd`)
- `crosses_land` ✅ measured 0.1 ms
- `crosses_obstacle` (similar)
- `is_restricted` (point-in-polygon — usually fast)
- `available_depth` — **lookup against ENC `DEPARE` polygons + GEBCO
  bilinear interpolation. NOT measured in the prior session.**
- objective cost calc

If `available_depth` is doing a netCDF read or a non-cached GEBCO
lookup per call, it could easily be 1–10 ms each. **Profile this
first.**

### 2. `LineString.buffer(margin_nm/60)` is created on every `crosses_land` call

`_crosses_layer` constructs a fresh `seg = LineString([...])` then
`probe = seg.buffer(...)` for every call. shapely buffer is not free
— it generates a polygon of ~16 vertices per call. With ~6 000
crosses_land + crosses_obstacle calls per step that's 6 000 fresh
polygons per step. Could be material; needs measuring.

Mitigation if real: cache the probe shape per (a, b, margin_nm)
endpoint pair (the `_segment_cache` already keys on this) — share
the probe geometry too, not just the bool result. Or precompute the
probe outside the hot loop.

### 3. STRtree.query on a buffered probe re-allocates per call

`tree.query(probe)` returns indices of geoms whose bounding boxes
overlap the probe's. shapely 2.x STRtree is C-backed and should be
microsecond-fast, but with thousands of geoms in the tree the result
arrays add up.

### 4. Frontier is bigger than expected

`sector_prune` keeps up to 72 points with a min-floor of 20. If the
frontier is hitting the 72 ceiling on every step (likely in
constrained coastal water where the prune doesn't merge), the
per-step propagation count is:

```
72 frontier × 50 headings = 3 600 propagations
× 4 spatial tests each      = 14 400 chart queries / step
× 0.1 ms each               = 1.4 s — still not 7
```

But add forecast/polar/geo costs and you're closer. **Run with a
metrics snapshot of `bv.router.propagations_per_step`** and see what
the actual number is on a real-mode run.

### 5. Python-level overhead per propagation

The per-propagation work is a long sequence of Python function calls
(each ~1 µs) plus dataclass `IsochronePoint` instantiation (also
~1 µs). 14 000 of these per step is 14–30 ms baseline before any
real work. Not a cliff, but contributes.

### 6. GIL contention or memory thrashing

Less likely under `ProcessPoolExecutor` (each worker has its own
GIL), but worth ruling out: `top` / `htop` while a real-mode voyage
runs to see if any worker is in IOWAIT or swapping.

## Recommended investigation plan

1. **Profile a single real-mode `plan_candidate` call.** Drop a
   `cProfile` wrapper around it in a test script (the prior session
   has a working in-container script — see "Reproduction" below).
   Sort by `cumulative` and look at the top 20 lines.

2. **Confirm `available_depth` per-call cost.** If it's >0.1 ms, it
   may need its own LRU cache (similar to the segment cache). The
   current `chart_depth` is already O(1) in the shapely DEPARE STRtree
   but GEBCO bilinear could be the slow path.

3. **Measure `LineString.buffer` separately** — `timeit` 10 000
   constructions in isolation. If it's a microsecond each it's fine;
   if it's tens of microseconds the buffered-probe construction is a
   real chunk of the budget.

4. **Inspect actual frontier sizes mid-run.** The
   `bv.router.propagations_per_step` histogram is already emitted to
   OTel. Submit a real-mode voyage and read the histogram values from
   the running container. If average frontier × fan = 14 000 +
   propagations, that's where most of the time is going.

5. **Once the bottleneck is found, fix it.** Likely candidates:
   - Cache `available_depth` per (lat, lon) quantized.
   - Cache or reuse the probe polygon construction.
   - Reduce frontier size — the polygon-based prune from doc 17 step
     4 is implemented but disabled by default; flipping it on may
     drop frontier size meaningfully.
   - Reduce heading fan in fine mode (`heading_fan_fine` is already
     ~55, might shrink to 30 without hurting quality).

## Reproduction

Run inside the container so you're hitting the same shapely / numpy
versions as production. Working bbox + chart data is already in
`/data/charts/` (mounted from the dev volume).

```bash
docker exec -it better-voyage python -c "
import time
from datetime import datetime, UTC
from pathlib import Path
import numpy as np

from app.services.charts import ChartStore
from app.services.forecast_field import ForecastField
from app.services.polars import Polar, DEFAULT_POLAR_PATH
from app.services.router import BoatLimits, plan_candidate, RouterError

origin = (37.7, -76.2)        # offshore Chesapeake
dest   = (38.3, -76.4)        # ~50 nm NW
bbox   = (37.4, -76.7, 38.5, -75.9)

store = ChartStore(
    base_dir=Path('/data/charts'),
    gebco_path=Path('/data/charts/gebco/GEBCO_2024_sub_ice_topo.nc'),
)
store.load_existing_for_bbox(bbox)

field = ForecastField(grid_res_deg=0.5)
field.lat_grid = np.array([bbox[0], bbox[2]], dtype=float)
field.lon_grid = np.array([bbox[1], bbox[3]], dtype=float)
start = datetime(2026, 4, 27, 0, 0, tzinfo=UTC)
field.time_grid = np.array(
    [np.datetime64(start.strftime('%Y-%m-%dT%H:%M:%S'), 's')
     + np.timedelta64(h, 'h') for h in range(36)],
    dtype='datetime64[s]',
)
shape = (2, 2, 36)
field.data = {k: np.full(shape, v) for k, v in {
    'wind_speed_kts': 12.0, 'wind_dir_deg': 270.0,
    'wind_gust_kts': 15.0, 'wave_height_m': 0.5,
    'wave_period_s': 4.0,  'wave_dir_deg': 270.0,
    'current_speed_kts': 0.0, 'current_dir_deg': 0.0,
}.items()}
polar = Polar.load(DEFAULT_POLAR_PATH)
boat = BoatLimits()

# Wrap with cProfile and dump to /tmp/route.prof
import cProfile
pr = cProfile.Profile()
pr.enable()
try:
    plan_candidate(
        origin=origin, destination=dest, depart_at=start,
        polar=polar, forecast=field, charts=store, boat=boat,
        wallclock_budget_s=180.0, safety_margin_land_nm=0.1,
    )
except RouterError as e:
    print(f'expected: {e.code}')
pr.disable()
pr.dump_stats('/tmp/route.prof')
print('profile written to /tmp/route.prof')
"

# Then inspect:
docker exec better-voyage python -c "
import pstats
p = pstats.Stats('/tmp/route.prof')
p.sort_stats('cumulative').print_stats(40)
"
```

Expected outcome on this synthetic forecast: `ROUTE_TIMEOUT` after
~24 steps in 180 s. The profile snapshot is what to chase.

## Files / functions to look at first

- `app/services/router.py::plan_candidate` — the main loop. Look for
  the per-propagation block (TWA → BSP → advance → 4 chart calls →
  cost → append).
- `app/services/router.py::sector_prune` — frontier reduction. Default.
- `app/services/router.py::polygon_prune` — experimental Hagiwara
  Normalize/Merge alternative. Disabled by default. Worth A/B'ing
  once the bottleneck is known.
- `app/services/charts.py::_crosses_layer` — buffered probe + STRtree
  query. The `LineString.buffer` allocation lives here.
- `app/services/charts.py::available_depth`,
  `app/services/charts.py::chart_depth` — depth lookup. Possibly the
  slow path; not yet measured.
- `app/services/forecast_field.py::ForecastField.at` — bilinear
  spatial + linear temporal interpolation. Should be numpy-fast but
  worth confirming it isn't allocating heavily per call.
- `app/services/geo.py::advance` (and the `pyproj.Geod` it calls).
  Each propagation does at least 2 of these (boat speed + current
  drift). Measure individually.

## Non-goals for this investigation

- **Don't** revisit the architectural choice between isochrone and
  base-path-then-corridor; doc 17 settled that.
- **Don't** chase the LineString-as-land bug — fixed at `e564487`.
- **Don't** "fix" the user's marina origin — separate concern; the
  endpoint snap puts the boat 0.06 nm from shore and the depth
  constraint then forecloses most headings. Fix is to require deeper
  clearance from the snap; orthogonal to this doc.
- **Don't** revisit the wallclock budget choice (15 min per candidate,
  2 h per voyage) — those are right for the "submit and come back"
  UX even if individual calls are slow.

## Definition of done

- A profile run on a representative real-mode voyage names the top
  three time consumers with their cumulative-time fractions.
- One or two targeted optimizations applied to the dominant
  consumers.
- Re-measured: 7 s/step → some target. A 2 s/step would feel
  reasonable; 1 s/step would feel great.
- Annapolis → St-Michaels real-mode goes from ~10 min to under 5 min
  for the 4-worker parallel case.

## Prior-session context

- Last commit on the parallel-routing path: `c14bdd0`.
- LineString-as-land fix: `e564487`.
- Container is `better-voyage:dev` on port 8000 with `--reload` and a
  `./app` volume mount; restart with `docker compose down && docker
  compose up -d --build` if reload misses.
- The dev uvicorn at port 8765 is **not** what the user's browser
  hits — that was a confusion in the prior session, don't repeat.
