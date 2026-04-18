# 05 — Scoring

**Status:** draft

Scoring assigns a comparable 0–100 number to each completed route so
the user can pick among candidates. Scoring is **pure, deterministic,
unit-testable**: given a decimated route and its `LegEnvironment`s,
same output every time. Changes are gated by golden-file tests
(doc 12).

## Objective vs. score

Two distinct numbers — don't conflate them.

- **Router objective function** (doc 04) drives what the isochrone
  search is optimizing for *during routing*. It's internal to the
  router, selected per-request (`fastest` / `comfortable` /
  `short_tacks`), and never exposed as a number to the user.
- **Candidate score** (this doc) is a 0–100 post-hoc summary. It
  compares *completed* routes using consistent components, regardless
  of which objective produced them.

A route optimized for `fastest` may score lower than one optimized for
`comfortable` — that's the point. The score reflects the passage
experience; the objective reflects what the algorithm was told to
minimize.

## Hard limits belong to routing

Earlier drafts treated hard limits (`max_wind_kts`, `max_seas_m`,
`min_depth_m`) as a disqualification step in scoring. They have moved
to the router (doc 04): any frontier point whose env violates a hard
limit is never propagated. If no feasible route exists, the candidate
returns `ROUTE_LIMIT_EXCEEDED` and never reaches scoring.

Scoring only reports how *good* completed passages are. "Bad" and
"impossible" are different concepts; we keep them separate.

## Sub-scores

Each sub-score is `env → float [0..1]`, higher is better. Composed by
weighted mean.

### Wind

TWS comfort curve, peaks ~10–15 kt true:

| TWS      | score                    |
| -------- | ------------------------ |
| 0 kt     | 0.3                      |
| 10–15 kt | 1.0                      |
| 20 kt    | 0.7                      |
| 25 kt    | 0.4                      |
| >30 kt   | (routing rejected — N/A) |

Direction multiplier on the speed score:

| TWA               | ×    |
| ----------------- | ---- |
| Beam 70–110°      | 1.0  |
| Close 45–70°      | 0.85 |
| Run 150–180°      | 0.8  |
| Close-hauled <45° | 0.5  |

### Waves

| Hs     | score                    |
| ------ | ------------------------ |
| <0.5 m | 1.0                      |
| 1.0 m  | 0.9                      |
| 1.5 m  | 0.75                     |
| 2.0 m  | 0.5                      |
| 2.5 m  | 0.25                     |
| >3.0 m | (routing rejected — N/A) |

Additional penalty if wave direction is forward of beam and
co-aligned with wind.

### Swell

`steepness = height / period`

| steepness | score              |
| --------- | ------------------ |
| <0.1      | 1.0 (long rollers) |
| 0.1–0.2   | 0.7                |
| >0.3      | 0.2                |

### Current

Along-track projection of surface current.

| along-track | score |
| ----------- | ----- |
| +2 kt       | 1.0   |
| 0 kt        | 0.6   |
| -2 kt       | 0.2   |

Linear between.

### Tide (shallow-waypoint check)

Only for routes whose arrival rtept has
`wpt.bv:depthM < boat.draft_m + 1.5 m`:

- clearance < `boat.min_depth_m` at planned arrival → handled as a
  hard limit by the router; never reaches scoring.
- 0.5–1.0 m clearance → 0.4
- > 1.5 m clearance → 1.0

If no shallow waypoint involved: default 1.0.

### Comfort (composite)

- Night-sailing penalty: if the leg spans local 22:00–06:00 and
  `boat.night_sailing_ok = False`, multiply by 0.5.
- Duration penalty: legs > 12 h take a small hit.

### Speed made good (SMG)

Measures how well the router used the conditions, not raw speed.

`smg_ratio = actual_smg / best_case_smg` where `best_case_smg` is the
polar's best SMG at the best wind the route actually saw.

| smg_ratio | score |
| --------- | ----- |
| ≥0.9      | 1.0   |
| 0.6       | 0.5   |
| <0.4      | 0.1   |

## Default weights

```
wind:    0.30
waves:   0.20
swell:   0.10
current: 0.10
tide:    0.10  (proportionally redistributed if no shallow wpt)
comfort: 0.10
smg:     0.10
```

Weights are named constants in `app/services/scorer.py`. Not a
per-request knob in MVP — if a user needs different priorities, they
change the router *objective*, which actually affects the route.

## Leg score → candidate score

- Weighted mean of per-leg scores, weighted by leg duration in hours.
- Scaled to 0–100 at the end.
- No further bonuses or penalties — contingencies and NL summaries are
  separate concerns.

## Determinism

- No wall-clock reads.
- No randomness.
- Golden-file tests pin expected scores for fixture voyages.

## Observability

`scorer.score_leg` and `scorer.score_candidate` emit spans and record:

- `bv.scoring.component` histogram, attribute `component`
- `bv.scoring.total` histogram
- Structured log `scoring.done` with the full components dict (debug
  level).
