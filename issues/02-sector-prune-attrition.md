# Isochrone frontier collapses mid-route

## Problem

Even with both endpoints confirmed in open water (e.g. `(37.70, -76.15) → (38.20, -76.30)`, both ≥3 nm from land, ≥12 m deep), most candidates fail with `ROUTE_NO_COVERAGE` after ~15 steps. The pre-prune frontier is healthy for a few steps (hundreds of propagations), then `sector_prune` starts returning very small sets, the frontier thins to single digits, and eventually three consecutive steps produce zero propagations — `RouterError("ROUTE_NO_COVERAGE")`.

Typical trajectory observed with the live ChartStore and a synthetic constant wind (single candidate, graduated stepping enabled):

```
step 1  |frontier|=  11 lead=43.2nm
step 4  |frontier|= 995 lead=42.3nm
step 6  |frontier|=  37 lead=43.2nm   (fine→coarse switch)
step 8  |frontier|=   9 lead=34.8nm
step 11 |frontier|=  10 lead=31.7nm
step 14 |frontier|=   3 lead=34.0nm
step 15 |frontier|=   3 lead=31.8nm
FAIL ROUTE_NO_COVERAGE
```

Progress toward destination is real for the first few coarse steps (43 → 32 nm), then the frontier is too small to thread around whatever obstacle (land, shallow polygon, restricted area) is next, and routing collapses.

## How to validate a solution

1. The `(37.70, -76.15) → (38.20, -76.30)` voyage completes with at least one candidate surviving under default forecast/boat.
2. A broader set of open-bay Chesapeake voyages (e.g. Annapolis ↔ Solomons, Kilmarnock ↔ St. Michaels) complete end-to-end.
3. Per-candidate wallclock stays bounded — no runs that grind at max_steps for minutes (which is the tail-end symptom of the same attrition: frontier doesn't die, but it also doesn't reach destination).
4. Don't regress `tests/unit/test_router.py::test_router_reaches_destination_on_beam_reach` or the existing near-shore graduated-stepping test.

## Possible solutions

- **Widen `sector_prune` half-width**, currently 90° (rejects anything with relative bearing >90° from centroid→destination). In narrow waters the viable fan can point obliquely; a threshold of 120–135° would preserve points that are still making axial progress. Risk: more points survive, router gets slower — need to cap.

- **Top-N by axial progress instead of sector bucketing.** Replace one-best-per-sector with "keep the `n` points with highest `distance_nm_toward_destination`." Simpler, guarantees frontier size ≥ min(n, |frontier|), avoids the "sector went empty" failure mode. Downside: less lateral diversity, may miss tacking branches.

- **Allow the frontier to persist across empty-propagation steps** before raising `ROUTE_NO_COVERAGE`. Currently 3 consecutive empty steps are fatal. Bumping that, or letting a shrunk frontier re-expand on the next step with a fresh fan, might recover from single-step obstructions.

- **Increase fan density when the frontier shrinks below a threshold.** Symmetric with the near-shore graduated stepping: if `|frontier| < 10`, expand more headings next step. Targets the specific failure without slowing the common case.

## Useful context

- Relevant code: `sector_prune` in `app/services/router.py:155`. The main loop's empty-frontier handling is in the same file around line 343–355.
- `heading_fan` (default) returns ~50 headings; `heading_fan_fine` returns ~55. Fan density isn't the current bottleneck — per-point propagation checks are fast (~0.5–1 ms per chart query, benchmarked).
- `sector_prune`'s centroid-relative axis calculation assumes the frontier is ahead of its centroid. In a long, thin frontier strung along a narrow bay, this can misclassify many points as "behind" even when they're still north of origin. Worth inspecting with an instrumented run.
- My earlier instrumentation script (`/tmp/plan_test*.log` outputs during session on 2026-04-20) wrapped `sector_prune` to log `|frontier|`, `lead_to_dest`, `median_to_dest` per step. Same pattern will reproduce this failure deterministically against the cached `data/charts` ENC/OSM indices.
