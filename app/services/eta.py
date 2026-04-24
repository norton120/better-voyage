"""Voyage wallclock ETA estimator.

Two layers:

1. **Heuristic prior** — a fixed per-candidate baseline keyed on
   `(real_charts, distance_band)`, scaled by enumerated candidate
   count. Used unconditionally as the prior; numbers are calibrated
   from measured benchmarks (Annapolis→Norfolk/Newport/St-Michaels).

2. **Empirical refinement** — pulls observed wallclocks
   (`completed_at - started_at`) from past `status='done'` voyage rows
   in the same bucket, normalised to per-candidate seconds, and blends
   with the prior using a sample-size-weighted scheme that avoids the
   classic cold-start trap (zero data → extreme estimate):

   - `n < 3`: pure heuristic. Wide ±50% range.
   - `3 ≤ n < 10`: 50/50 blend of heuristic and observed median.
     Moderate ±30% range.
   - `n ≥ 10`: empirical median + p10/p90. Range from the data.

The bucket key is deliberately coarse (`real_charts × distance_band`
with four bands). Fine buckets would dilute the small sample sizes we
expect in practice; coarse buckets risk mixing dissimilar routes but
the prior catches that gracefully.

Used at two points:
- `POST /voyages` → compute ETA, return in the 202 payload.
- `_tick` during routing → refine ETA based on observed elapsed-so-far
  and remaining candidate count. This matters more than the initial
  estimate once routing is underway because the user is looking at a
  live number.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Literal

from sqlalchemy import select

from app.models.voyage import Voyage
from app.schemas.request import VoyageRequest
from app.services.geo import distance_nm

Basis = Literal["heuristic", "blended", "empirical", "live"]


@dataclass(frozen=True)
class EtaEstimate:
    eta_seconds: float          # central estimate
    eta_seconds_low: float      # optimistic bound
    eta_seconds_high: float     # pessimistic bound
    basis: Basis                # how this number was produced
    sample_size: int            # observed voyages that informed it


# Heuristic per-candidate baselines (seconds). Ordered roughly by
# compute cost. Calibrated from April 2026 benchmarks; see commit
# `feat(router,planner): lift wallclock caps` for context.
#
# Buckets: (real_charts, distance_band) where distance_band is one of
# "short" (<50 nm), "medium" (50–200 nm), "long" (200–500 nm),
# "ocean" (500+ nm). Null-charts runs are cheap; real-charts runs are
# dominated by shapely intersect cost and scale ~10x.
_PER_CANDIDATE_S: dict[tuple[bool, str], float] = {
    (False, "short"):   20.0,
    (False, "medium"):  30.0,
    (False, "long"):    80.0,
    (False, "ocean"):  150.0,
    (True,  "short"):  200.0,
    (True,  "medium"): 400.0,
    (True,  "long"):   800.0,
    (True,  "ocean"): 1500.0,
}

# Window for empirical lookback. 30 days is long enough to gather
# meaningful samples on a low-traffic deployment without letting
# stale benchmarks (different charts release, different host) bias
# the estimate indefinitely.
_LOOKBACK_DAYS = 30
_MAX_OBSERVATIONS = 50
_MIN_OBS_FOR_BLEND = 3
_MIN_OBS_FOR_EMPIRICAL = 10


def _distance_band(nm: float) -> str:
    if nm < 50:
        return "short"
    if nm < 200:
        return "medium"
    if nm < 500:
        return "long"
    return "ocean"


def _bucket(req: VoyageRequest, *, real_charts: bool) -> tuple[bool, str]:
    nm = distance_nm(
        req.origin.lat, req.origin.lon,
        req.destination.lat, req.destination.lon,
    )
    return (real_charts, _distance_band(nm))


def heuristic_per_candidate_s(req: VoyageRequest, *, real_charts: bool) -> float:
    """Baseline per-candidate wallclock in seconds from the heuristic table."""
    return _PER_CANDIDATE_S[_bucket(req, real_charts=real_charts)]


def heuristic_estimate(
    req: VoyageRequest, *, real_charts: bool, n_candidates: int
) -> EtaEstimate:
    per = heuristic_per_candidate_s(req, real_charts=real_charts)
    eta = per * max(1, n_candidates)
    # Cold-start: wide range reflects that we don't actually know.
    return EtaEstimate(
        eta_seconds=eta,
        eta_seconds_low=eta * 0.5,
        eta_seconds_high=eta * 1.5,
        basis="heuristic",
        sample_size=0,
    )


def _per_candidate_from_row(row: Voyage) -> float | None:
    """Observed per-candidate seconds for a completed voyage, or None
    if we can't compute it cleanly."""
    if row.started_at is None or row.completed_at is None:
        return None
    try:
        req_data = json.loads(row.request_json)
        req = VoyageRequest.model_validate(req_data)
    except Exception:
        return None
    # `effective_routed_count` mirrors the planner's cap + adaptive-step
    # logic. Imported lazily to avoid the circular with services.planner.
    # For legacy pre-cap voyages this slightly inflates per-candidate
    # seconds (divisor = 10 vs. historically-routed 56) but biases
    # estimates high, which is the safe direction. Aged out of the
    # 30-day lookback window in a few weeks.
    from app.services.planner import effective_routed_count
    n = effective_routed_count(req)
    if n <= 0:
        return None
    elapsed = (row.completed_at - row.started_at).total_seconds()
    if elapsed <= 0:
        return None
    return elapsed / n


async def _observed_per_candidate_samples(
    bucket: tuple[bool, str],
    *,
    session,
    real_charts: bool,
) -> list[float]:
    """Observed per-candidate seconds from recent matching voyages."""
    cutoff = datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)
    stmt = (
        select(Voyage)
        .where(Voyage.status == "done")
        .where(Voyage.completed_at >= cutoff)
        .order_by(Voyage.completed_at.desc())
        .limit(_MAX_OBSERVATIONS * 3)  # overfetch; we filter by bucket below
    )
    rows = (await session.execute(stmt)).scalars().all()
    samples: list[float] = []
    for row in rows:
        try:
            req_data = json.loads(row.request_json)
            req = VoyageRequest.model_validate(req_data)
        except Exception:
            continue
        if _bucket(req, real_charts=real_charts) != bucket:
            continue
        per = _per_candidate_from_row(row)
        if per is not None:
            samples.append(per)
        if len(samples) >= _MAX_OBSERVATIONS:
            break
    return samples


def _percentile(xs: list[float], p: float) -> float:
    """Simple nearest-rank percentile. Input doesn't need to be sorted."""
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[k]


async def estimate_eta(
    req: VoyageRequest,
    *,
    real_charts: bool,
    n_candidates: int,
    session,
) -> EtaEstimate:
    """Blended prior + empirical ETA. Cold-start safe: with no data,
    collapses to `heuristic_estimate`."""
    prior = heuristic_estimate(req, real_charts=real_charts, n_candidates=n_candidates)
    bucket = _bucket(req, real_charts=real_charts)
    samples = await _observed_per_candidate_samples(
        bucket, session=session, real_charts=real_charts
    )
    n = len(samples)
    if n < _MIN_OBS_FOR_BLEND:
        return prior

    observed_med = median(samples)
    if n < _MIN_OBS_FOR_EMPIRICAL:
        prior_per = prior.eta_seconds / max(1, n_candidates)
        blended_per = 0.5 * prior_per + 0.5 * observed_med
        eta = blended_per * max(1, n_candidates)
        return EtaEstimate(
            eta_seconds=eta,
            eta_seconds_low=eta * 0.7,
            eta_seconds_high=eta * 1.3,
            basis="blended",
            sample_size=n,
        )

    # Fully empirical. p10/p90 bounds from the observed distribution.
    eta = observed_med * max(1, n_candidates)
    return EtaEstimate(
        eta_seconds=eta,
        eta_seconds_low=_percentile(samples, 0.10) * max(1, n_candidates),
        eta_seconds_high=_percentile(samples, 0.90) * max(1, n_candidates),
        basis="empirical",
        sample_size=n,
    )


def live_estimate(
    *,
    elapsed_s: float,
    candidates_done: int,
    candidates_total: int,
    fallback: EtaEstimate,
) -> EtaEstimate:
    """Refine the ETA mid-routing from observed per-candidate rate.

    Called from `_tick` after each candidate completes. Once we have
    at least one completion we know the real cost on this host with
    this forecast, which is strictly better than the prior. Until
    then, fall back to the submit-time estimate (`fallback`).
    """
    if candidates_done <= 0 or candidates_total <= 0:
        return fallback
    per_candidate = elapsed_s / candidates_done
    remaining = max(0, candidates_total - candidates_done)
    eta = remaining * per_candidate
    return EtaEstimate(
        eta_seconds=eta,
        eta_seconds_low=eta * 0.8,
        eta_seconds_high=eta * 1.3,
        basis="live",
        sample_size=candidates_done,
    )


def now_monotonic_s() -> float:
    """Indirect for tests; real impl is `time.monotonic`."""
    return time.monotonic()
