# 17 — Isochrone overhaul

**Status:** draft
**Supersedes parts of:** [04-routing.md § Pruning](./04-routing.md#pruning),
[04-routing.md § Chart integration](./04-routing.md#chart-integration).

## Why this doc exists

The router has been thrashing on coastal / narrow-channel / bbox-edge
cases (recent commits: `fix(router,planner): produce routes in coastal
bboxes`, `fix(router): revert sector_prune half-width to 90°`,
`feat(charts,ui): submit-time land check`). The question was whether
the fundamental architecture is wrong — specifically, whether we
should pre-compute a land-avoiding base path first and then weather-
route within a corridor.

**Research conclusion: the architecture is right. The sector-prune
implementation is the main problem.** Every serious sailboat router
(weather_routing_pi, qtVlm, LuckGrib, Expedition, Adrena, SailGrib) uses
weather-first isochrone expansion with land applied as a per-edge
constraint. "Base path then corridor" is a known dead end for sailing
because the optimum is *defined by* the weather — downwind you go low,
upwind you tack wide, a 200 nm detour for a favourable shift beats the
rhumb line. A great-circle corridor prunes out the actual answer.

This doc captures the diagnosis, the comparison with the reference
implementation, and the four-step plan.

## Reference implementation: weather_routing_pi

OpenCPN's weather_routing_pi is the closest open-source equivalent and
a good line-by-line reference. Its data model:

- `Position` — lat/lon + `parent` pointer (time-wise parent in the
  previous isochrone) + `parent_heading`, `tack_count`, `polar_idx`.
  Doubly-linked `prev`/`next` into a closed polygon.
- `IsoRoute` — one reachable polygon. `SkipPosition` skip-list over the
  polygon for fast segment tests. `direction` = +1 normal / −1 "hole"
  (inverted region). `children` = nested inverted regions.
- `IsoChron` — list of `IsoRoute`s at time `t`.
- `RouteMap` — list of `IsoChron`s plus `RouteMapConfiguration`.

The main loop (`RouteMap::Propagate` in `src/RouteMap.cpp`) advances the
front one step: each `Position` in the current chron fans out
`DegreeSteps` TWAs, integrates forward via `rk_step()` (RK4), and
produces a new closed polygon. Then:

1. `IsoRoute::Normalize` uses segment-vs-segment intersection tests to
   eliminate self-intersections — critical for sailboats because
   non-convex polars create lobes that cross each other.
2. `ReduceList` repeatedly calls `Merge()` to combine overlapping
   polygons and extract inverted "hole" regions as child routes.
3. `ReduceClosePoints` thins dense nodes.

This is the Hagiwara "modified isochrone" non-convex fix. It's what
preserves tactical points like "tack far south to catch the shift."

Land avoidance (`ConstraintChecker::CheckLandConstraint` in
`src/ConstraintChecker.cpp`) is a per-edge test against GSHHS
(`PlugIn_GSHHS_CrossesLand`), with a segment-keyed LRU cache
(~10,000 entries, maintained each step). A `SafetyMarginLand`
(0.1–2 nm) buffer widens the rejected corridor to absorb coastline
coarseness.

## better-voyage vs weather_routing_pi

| Concern | weather_routing_pi | better-voyage (`app/services/router.py`) | Verdict |
|---|---|---|---|
| Core algorithm | Hagiwara modified isochrone | Hagiwara isochrone | ✅ same |
| Land as per-edge constraint | `ConstraintChecker::CheckLandConstraint` | `charts.crosses_land()` at L372 | ✅ same |
| Non-convex front handling | `IsoRoute::Normalize` + `Merge` (full polygon, split inverted lobes as children) | **72-sector angular prune** with 20-point floor (L172–) | ⚠️ **likely root cause of "frontier collapse"** |
| Segment land-test LRU | ~10k entry LRU keyed on segment | None — naive per-call | ⚠️ missing |
| `SafetyMarginLand` | Yes (0.1–2 nm) | None | ⚠️ missing |
| Adaptive Δt | 10–60 **seconds**, shrinks at boundaries | 30 min nominal / 10 min coastal | ⚠️ much too coarse |
| Currents | Added in dynamic step | Added in dynamic step | ✅ same |
| Integration | RK4 (`rk_step()`) | Newton step | ⏭ defer |
| Shoreline data | GSHHS | NOAA ENC + OSM + GEBCO | ✅ better when present, worse at seams |

## Failure pattern attribution

Mapping recent commit churn to the table:

| Recent failure | Direct cause | Root cause in the table |
|---|---|---|
| "frontier collapse" → needed 20-point floor | Sector pruning discards oblique-progress points | **Non-convex front handling** — `Normalize/Merge` preserves them naturally |
| `sector_prune` half-width thrash (90°→120°→90°) | Narrow channels need wider acceptance, but wider pushes outside forecast bbox | **Non-convex front handling** — the geometric front doesn't need an angular half-width at all |
| Coastal bbox bugs, forecast NaN corners | Fine time step is still 10 min ≈ 1 nm of uncheckable motion | **Coarse Δt** — 60 s brings it under any reasonable chart cell |
| `crosses_land` hot-path cost | Segment test is naive | **Missing LRU** |
| "cuts a corner" in narrow passages | No safety buffer around land edges | **Missing `SafetyMarginLand`** |

## Plan: four changes, ordered by ascending risk

### 1. Segment land-test LRU cache (lowest risk)

Wrap `ChartStore.crosses_land(a, b)` with an LRU keyed on quantized
endpoints (e.g. 4 decimal degrees ≈ 11 m). ~10,000 entries. Rebuild is
not needed — entries are immutable since charts don't change mid-run.

This is load-bearing for change 3 (finer Δt multiplies call volume).
Do it first.

### 2. `SafetyMarginLand` parameter

Add `safety_margin_land_nm: float = 0.5` to router config. Implement as
a buffered segment test: inflate the segment to a thin polygon of the
given half-width before the land intersection check. Coarse coastline
data (GEBCO grid cells, OSM coastline decimation) produces apparent
"navigable water" that is actually beach — this buffer absorbs that.

### 3. Drop coastal Δt to ~60 s

Reduce the coastal time step from 10 min to 60 s when within N nm
(default 2 nm) of land **or** of the destination. The LRU from change 1
makes the extra land tests cheap. Offshore Δt stays at 30 min.

Expected change to `plan_candidate` in `app/services/router.py`:
rework `_frontier_near_shore` to classify into offshore / near-shore /
near-destination bands rather than a bool. Budget remains ~520 k
propagations because most time is spent offshore.

### 4. Replace sector-prune with polygon Normalize/Merge (highest leverage, biggest change)

Swap `sector_prune` (router.py L172–) for a Hagiwara modified-isochrone
polygon pass. Mirror weather_routing_pi/src/IsoRoute.cpp:

- Represent the isochrone as one or more closed polygons (`IsoRoute`
  analogues).
- `Normalize` — segment-vs-segment intersection pass to eliminate
  self-intersections, swap crossed endpoints.
- `Merge` / `ReduceList` — combine overlapping polygons; extract
  inverted "hole" regions as child routes.
- `ReduceClosePoints` — thin dense nodes (keep the current shapely
  STRtree).

Use shapely for polygon ops; it already ships (doc 04, deps list).
`Normalize` is the only non-trivial piece — segment-intersection with
swap — and is ~100 lines.

**Why last:** touches the hottest loop in the router, requires a
rethink of frontier typing (`list[IsochronePoint]` → polygon), and
needs its own golden-route regression before merge. Changes 1–3 buy us
breathing room to do this without the codebase on fire.

**Shipped so far (experimental):** `polygon_prune` in
`app/services/router.py` — a shapely-based v0 that walks the frontier
in insertion order (parent-major, heading-minor = a boundary walk of
the reachable set), applies `Polygon(ring).buffer(0)` to resolve
self-intersections, and remaps exterior-ring vertices back to
`IsochronePoint`s by nearest-neighbor. Gated behind
`plan_candidate(prune_mode="polygon")`; default stays `"sector"`.

**Known v0 limits (track when flipping default):**
1. No multi-polygon / hole support — a `MultiPolygon` output is
   collapsed to its largest piece, so tactical reachability in smaller
   pieces is lost. weather_routing_pi's `ReduceList` preserves these as
   child `IsoRoute`s; we don't yet.
2. Nearest-neighbor remap can pick a duplicate when `buffer(0)` inserts
   a new vertex on an existing edge. The `seen` set dedups but can
   still drop a distinct point in dense regions.
3. No `ReduceClosePoints` pass — polygons with thousands of vertices
   stay large. Add once the golden benchmarks show where density hurts.

**To flip the default:** run `tests/benchmarks/` under both modes, gate
on ≤2 % ETA delta and Fréchet shape similarity, and close out
limits 1–3 above.

## Out of scope (defer)

- **RK4 integration step.** Marginal accuracy gain; not what's breaking
  us.
- **Multi-polar `CrossOverRegion` sail changes.** Useful feature;
  orthogonal to the failure modes above.
- **Tidal harmonic atlas layered under the GRIB current field.** Doc 03
  already plans this; also orthogonal.
- **Pre-compute base path + corridor.** Explicitly rejected — see
  "Why this doc exists."

## Success criteria

- Every route in `tests/benchmarks/` routes to completion.
- Coastal/narrow-channel voyages (the ones that currently require the
  20-point frontier floor) succeed without the floor.
- No regression on offshore golden routes: ETA within ±2 %, route
  shape within Fréchet distance of the pinned fixture.
- Per-candidate wallclock stays within the 1–2 s laptop budget
  (doc 04 § Complexity & performance).

## References

- [rgleason/weather_routing_pi — `src/RouteMap.cpp`](https://github.com/rgleason/weather_routing_pi/blob/master/src/RouteMap.cpp)
- [rgleason/weather_routing_pi — `src/IsoRoute.cpp`](https://github.com/rgleason/weather_routing_pi/blob/master/src/IsoRoute.cpp)
- [rgleason/weather_routing_pi — `src/Position.cpp`](https://github.com/rgleason/weather_routing_pi/blob/master/src/Position.cpp)
- [rgleason/weather_routing_pi — `src/ConstraintChecker.cpp`](https://github.com/rgleason/weather_routing_pi/blob/master/src/ConstraintChecker.cpp)
- [LuckGrib — Isochrones](https://routing.luckgrib.com/intro/isochrones/index.html)
- [Szłapczyńska & Śmierzchalski — Adopted isochrone method](https://www.researchgate.net/publication/238194267)
- [ScienceDirect 2025 — State of the art in weather routing](https://www.sciencedirect.com/science/article/pii/S0029801825009114)
