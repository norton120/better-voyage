"""ETA estimator: heuristic prior, empirical blend, live refinement.

Contract: `estimate_eta` must be cold-start safe — with zero past
voyages it returns the heuristic prior, not a divide-by-zero or an
empty-data artifact.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.request import Coord, TimeWindow, VoyageRequest
from app.services.eta import (
    EtaEstimate,
    estimate_eta,
    heuristic_estimate,
    live_estimate,
)


def _req(
    origin: tuple[float, float] = (38.978, -76.492),
    destination: tuple[float, float] = (36.915, -76.32),
) -> VoyageRequest:
    return VoyageRequest(
        origin=Coord(lat=origin[0], lon=origin[1], name="O"),
        destination=Coord(lat=destination[0], lon=destination[1], name="D"),
        window=TimeWindow(
            start_at=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
            end_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
            tz="UTC",
        ),
        boat_profile_name="default",
        objective="fastest",
        max_candidates=3,
    )


def test_heuristic_produces_wide_range() -> None:
    est = heuristic_estimate(_req(), real_charts=False, n_candidates=3)
    assert est.basis == "heuristic"
    assert est.sample_size == 0
    assert est.eta_seconds_low < est.eta_seconds < est.eta_seconds_high
    # ±50% by spec.
    assert est.eta_seconds_low == pytest.approx(est.eta_seconds * 0.5)
    assert est.eta_seconds_high == pytest.approx(est.eta_seconds * 1.5)


def test_real_charts_costs_more_than_null() -> None:
    null_est = heuristic_estimate(_req(), real_charts=False, n_candidates=3)
    real_est = heuristic_estimate(_req(), real_charts=True, n_candidates=3)
    assert real_est.eta_seconds > 3 * null_est.eta_seconds


def test_longer_distance_costs_more_than_short() -> None:
    short = heuristic_estimate(
        _req(origin=(38.5, -76.5), destination=(38.6, -76.5)),  # ~6 nm
        real_charts=False, n_candidates=3,
    )
    long = heuristic_estimate(
        _req(origin=(38.5, -76.5), destination=(42.5, -71.0)),  # ~320 nm
        real_charts=False, n_candidates=3,
    )
    assert long.eta_seconds > short.eta_seconds


class _FakeRow:
    """Minimum shape of a Voyage row needed by `_per_candidate_from_row`."""

    def __init__(
        self,
        *,
        started_at: datetime,
        completed_at: datetime,
        req: VoyageRequest,
    ) -> None:
        self.started_at = started_at
        self.completed_at = completed_at
        self.request_json = req.model_dump_json()


def _fake_session_with(rows: list[_FakeRow]) -> MagicMock:
    scalars = MagicMock()
    scalars.all.return_value = rows
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    session = MagicMock()
    session.execute = AsyncMock(return_value=exec_result)
    return session


@pytest.mark.asyncio
async def test_cold_start_falls_back_to_heuristic() -> None:
    """Zero past voyages → heuristic, never a divide-by-zero."""
    session = _fake_session_with([])
    est = await estimate_eta(
        _req(), real_charts=False, n_candidates=3, session=session
    )
    assert est.basis == "heuristic"
    assert est.sample_size == 0


@pytest.mark.asyncio
async def test_blends_at_small_sample() -> None:
    """3 ≤ n < 10 → 50/50 blend of heuristic and observed median.

    Observed per-candidate seconds of 100 — very different from any
    heuristic value — should pull the estimate toward 100 × N but not
    all the way there.
    """
    req = _req()
    # Per-candidate must match effective_routed_count (post-cap)
    # so the observed rate we bake into the fake elapsed time is
    # interpretable.
    from app.services.planner import effective_routed_count

    n_routed = effective_routed_count(req)
    rows = [
        _FakeRow(
            started_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
                + timedelta(seconds=100 * n_routed),
            req=req,
        )
        for _ in range(4)
    ]
    session = _fake_session_with(rows)
    est = await estimate_eta(
        req, real_charts=False, n_candidates=3, session=session
    )
    assert est.basis == "blended"
    assert est.sample_size == 4
    # Each sample is 100 s/candidate observed. For n=3 candidates
    # empirical alone would be 300 s. The blend pulls the heuristic
    # (medium-null = 30 s/cand × 3 = 90 s) toward that.
    empirical_full = 100 * 3
    prior_full = 30 * 3
    expected = 0.5 * prior_full + 0.5 * empirical_full
    assert est.eta_seconds == pytest.approx(expected, rel=0.2)


@pytest.mark.asyncio
async def test_empirical_at_large_sample() -> None:
    """n ≥ 10 → empirical median + p10/p90 bounds."""
    req = _req()
    from app.services.planner import effective_routed_count

    n_routed = effective_routed_count(req)
    rows = [
        _FakeRow(
            started_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
                + timedelta(seconds=120 * n_routed),
            req=req,
        )
        for _ in range(12)
    ]
    session = _fake_session_with(rows)
    est = await estimate_eta(
        req, real_charts=False, n_candidates=5, session=session
    )
    assert est.basis == "empirical"
    assert est.sample_size == 12
    # All samples are identical at 120 s/candidate → median 120.
    assert est.eta_seconds == pytest.approx(120 * 5, rel=0.05)
    # Identical samples → p10 == p90 == median.
    assert est.eta_seconds_low == pytest.approx(est.eta_seconds, rel=0.05)


def test_live_estimate_refines_from_rate() -> None:
    """Once any candidate has completed we have a real rate to use."""
    fallback = EtaEstimate(600.0, 300.0, 900.0, "heuristic", 0)
    # 2 of 10 done, 60 s elapsed → 30 s/candidate → 8 × 30 = 240 s remaining.
    est = live_estimate(
        elapsed_s=60.0, candidates_done=2, candidates_total=10, fallback=fallback
    )
    assert est.basis == "live"
    assert est.eta_seconds == pytest.approx(240.0)
    assert est.eta_seconds_low < est.eta_seconds < est.eta_seconds_high


def test_live_estimate_uses_fallback_before_first_completion() -> None:
    fallback = EtaEstimate(600.0, 300.0, 900.0, "heuristic", 0)
    est = live_estimate(
        elapsed_s=10.0, candidates_done=0, candidates_total=10, fallback=fallback
    )
    assert est is fallback
