# 06 — Contingencies: backups, tap-outs, escape hatches

**Status:** draft

Every voyage should fail gracefully. This is a core differentiator vs.
a generic weather-window picker.

All three contingency kinds are serialized into the voyage GPX; the
routes among them are tagged with `bv:contingencyKind` and a structured
`bv:trigger` (see docs 01 and 09). OpenCPN shows them as separate
selectable routes; the API surfaces them as siblings of the primary
candidate.

## Three kinds

### 1. Backup anchorage

> "Your target anchorage is full, exposed, or wrong for the forecast.
>  Here's another one within X nm."

- Attached to the **destination**.
- Selected from POIs within `BACKUP_RADIUS_NM` (default 5).
- Filter by `type in ("anchorage", "marina", "harbor_of_refuge")` and
  `bv:shelterQuadrants` covering the forecast wind direction at
  planned arrival.
- Ranked by distance + shelter match.
- Emitted as an entry in `bv:backupDestinations` on the primary
  `<rte>` extensions (not a separate route — it's metadata about the
  terminal point).

### 2. Tap-out marina / refuge

> "If you bail mid-passage, here's where to go."

- Generated for each **decision point** rtept on the primary route.
  Doc 04 emits these automatically at every 4 h of elapsed time or
  when a downstream env threshold is crossed.
- Query the POI R-tree for candidates within `TAPOUT_DETOUR_NM`
  (default 8 nm) of the decision-point rtept. Filter by appropriate
  `type` and shelter.
- Rank by: (a) detour distance, (b) 24/7 accessibility, (c) shelter
  match to forecast wind quadrant at that rtept's `bv:plannedAt`.
- Keep top `TAPOUT_KEEP_TOP_N` (default 3).
- Attached as `bv:tapOut: list[{name, lat, lon, detour_nm, notes}]` on
  the decision-point rtept's extensions. **No separate `<rte>`** —
  tap-outs are annotations, not routes. OpenCPN shows them in the
  rtept's properties panel.

### 3. Escape-hatch route (isochrone re-route)

> "If seas build above 2 m on the next leg, here's an alternate route."

- For each decision-point rtept whose **downstream segment** has a
  predicted env crossing a threshold (`ESCAPE_SEAS_M` default 2.0,
  `ESCAPE_WIND_KTS` default 25), re-run the isochrone router (doc 04)
  from that rtept with:
  - **Modified endpoint** — the nearest `harbor_of_refuge` /
    `anchorage` POI, or an explicit fallback declared with
    `type="inside_alternative"` in `pois.gpx`.
  - **Tightened hard limits** — e.g., `max_seas_m` reduced by 0.5.
- Only emitted if the re-routed path differs meaningfully from the
  primary. Similarity is measured by discrete Fréchet distance over
  decimated points; routes whose frontier deviates less than
  `ESCAPE_DIVERGENCE_NM` (default 2 nm) are dropped.
- Emitted as a separate `<rte>` with:
  - `bv:contingencyKind = "escape_hatch_route"`
  - `bv:trigger = {seasMGt: 2.0, windKtsGt: 25, ...}` (actual threshold
    that fired)
  - `bv:parentRtept = "<name of decision-point rtept on primary>"`

## Selection pseudocode

```python
def derive_contingencies(
    primary: Route,
    pois: PoiIndex,
    forecast: ForecastField,
    boat: BoatProfile,
) -> list[Route]:
    out = []

    # (1) Backup anchorages (metadata on primary, no new <rte>)
    primary.extensions.bv.backupDestinations = find_backup_anchorages(
        primary.rtepts[-1], pois, forecast
    )

    # (2) Tap-outs (annotations on decision-point rtepts)
    for rtept in decision_points(primary):
        rtept.extensions.bv.tapOut = find_tapouts(
            rtept, pois, forecast, keep=TAPOUT_KEEP_TOP_N
        )

    # (3) Escape-hatch re-routes
    for rtept in decision_points(primary):
        trigger = env_trigger_if_risky(rtept, primary, forecast)
        if trigger is None:
            continue
        target = nearest_refuge(rtept, pois) or declared_alternative(rtept, pois)
        if target is None:
            continue
        alt = run_isochrone(
            start=rtept.coord,
            end=target.coord,
            depart_at=rtept.bv.plannedAt,
            boat=tightened(boat, trigger),
            forecast=forecast,
            objective="fastest",
        )
        if alt.ok and meaningfully_different(alt, primary, ESCAPE_DIVERGENCE_NM):
            out.append(
                alt.as_route(
                    contingency_kind="escape_hatch_route",
                    trigger=trigger,
                    parent_rtept=rtept.name,
                )
            )

    return out
```

## Presentation

- **API:** contingency routes appear in `voyage.routes[]` alongside
  primary candidates, each with `bv:contingencyKind`, `bv:trigger`, and
  a human-readable `desc`.
- **GPX:** each is a `<rte>` with a descriptive `<name>` like
  `"Candidate 1 — escape to Deltaville (seas > 2.0 m)"`. OpenCPN lists
  it alongside the primary route.
- **Tap-out and backup annotations** never get their own `<rte>`; they
  live as extensions on existing rtepts / rtes, rendered into the
  point's `<desc>` for plotter visibility.

## Thresholds

Named constants in `app/services/contingency.py`:

```
BACKUP_RADIUS_NM     = 5
TAPOUT_DETOUR_NM     = 8
TAPOUT_KEEP_TOP_N    = 3
ESCAPE_SEAS_M        = 2.0
ESCAPE_WIND_KTS      = 25
ESCAPE_DIVERGENCE_NM = 2.0
```

## Observability

- Span `contingency.derive` with `n_tapouts`, `n_escape_hatches`
  attributes.
- Each escape-hatch isochrone is a nested `router.plan_candidate` span
  with `bv.contingency.parent_rtept` attribute for correlation.
- Metric `bv.contingencies.emitted{kind}` counter.

## Notes

- "Bail forward" (continue to an earlier POI on the existing route
  rather than diverting off-route) — falls out uniformly since any POI
  on the primary is already visible to the tap-out selector.
- Per-POI call-ahead metadata (VHF, phone): lives in `bv:amenities` on
  the POI `<wpt>`; populate as we curate `pois.gpx`.
- Synchronous escape-hatch generation keeps the voyage document self-contained and lets us
  fail fast.
