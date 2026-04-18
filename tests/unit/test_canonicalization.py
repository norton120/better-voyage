"""Unit tests for request canonicalization + inputs_hash stability."""

from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.request import (
    Coord,
    TimeWindow,
    VoyageRequest,
    canonicalize,
    compute_inputs_hash,
)


def _req(**overrides) -> VoyageRequest:
    base = dict(
        origin=Coord(lat=38.9784, lon=-76.4922, name="Annapolis"),
        destination=Coord(lat=36.8467, lon=-76.2929, name="Norfolk"),
        window=TimeWindow(
            start_at=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
            end_at=datetime(2026, 4, 27, 10, 0, tzinfo=UTC),
        ),
        boat_profile_name="saltbreaker",
    )
    base.update(overrides)
    return VoyageRequest(**base)


def test_hash_stable_across_max_candidates() -> None:
    # `max_candidates` is a tuning knob, not a semantic input.
    assert compute_inputs_hash(_req(max_candidates=3)) == compute_inputs_hash(_req(max_candidates=10))


def test_hash_stable_across_subminute_time_jitter() -> None:
    start = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
    a = _req(window=TimeWindow(start_at=start, end_at=start.replace(day=27)))
    b = _req(
        window=TimeWindow(
            start_at=start.replace(second=42, microsecond=17),
            end_at=start.replace(day=27, second=3),
        )
    )
    assert compute_inputs_hash(a) == compute_inputs_hash(b)


def test_hash_changes_for_different_destination() -> None:
    h1 = compute_inputs_hash(_req())
    h2 = compute_inputs_hash(_req(destination=Coord(lat=37.0, lon=-76.0, name="Other")))
    assert h1 != h2


def test_canonicalize_is_deterministic() -> None:
    a = canonicalize(_req())
    b = canonicalize(_req())
    assert a == b
