"""Validation tests for VoyageRequest / TimeWindow (plan/10 §errors)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.request import Coord, TimeWindow, VoyageRequest


def _base(**overrides):
    defaults = dict(
        start_at=datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return defaults


def _req(**overrides):
    defaults = dict(
        origin=Coord(lat=38.5, lon=-76.5),
        destination=Coord(lat=38.5, lon=-76.0),
        window=TimeWindow(**_base()),
        boat_profile_name="default",
    )
    defaults.update(overrides)
    return VoyageRequest(**defaults)


def test_window_start_after_end_rejects() -> None:
    with pytest.raises(ValidationError, match="INVALID_WINDOW"):
        TimeWindow(
            start_at=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
        )


def test_window_equal_rejects() -> None:
    t = datetime(2026, 4, 18, 0, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="INVALID_WINDOW"):
        TimeWindow(start_at=t, end_at=t)


def test_window_longer_than_14_days_rejects() -> None:
    with pytest.raises(ValidationError, match="INVALID_WINDOW"):
        TimeWindow(
            start_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 4, 16, 0, 0, tzinfo=UTC),  # 15 days
        )


def test_window_14_days_accepted() -> None:
    TimeWindow(
        start_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 15, 0, 0, tzinfo=UTC),  # exactly 14 days
    )


def test_origin_equal_destination_rejects() -> None:
    with pytest.raises(ValidationError, match="INVALID_WINDOW"):
        _req(
            origin=Coord(lat=38.5, lon=-76.5),
            destination=Coord(lat=38.5, lon=-76.5),
        )


def test_valid_request_builds() -> None:
    req = _req()
    assert req.objective == "fastest"
    assert req.max_candidates == 5
