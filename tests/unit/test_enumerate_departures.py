"""Departure-grid enumeration tests."""

from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.request import Coord, TimeWindow, VoyageRequest
from app.services.planner import enumerate_departures


def _req(start: datetime, end: datetime) -> VoyageRequest:
    return VoyageRequest(
        origin=Coord(lat=38.5, lon=-76.5),
        destination=Coord(lat=38.5, lon=-76.0),
        window=TimeWindow(start_at=start, end_at=end, tz="UTC"),
        boat_profile_name="default",
    )


def test_hourly_grid_over_3_hours() -> None:
    start = datetime(2026, 4, 18, 0, 0, tzinfo=UTC)
    end = datetime(2026, 4, 18, 3, 0, tzinfo=UTC)
    ts = enumerate_departures(_req(start, end))
    # 00:00, 01:00, 02:00, 03:00
    assert len(ts) == 4
    assert ts[0] == start
    assert ts[-1] == end


def test_narrow_window_yields_single_departure() -> None:
    from datetime import timedelta

    start = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    # Less than an hour → only one departure at start.
    ts = enumerate_departures(_req(start, start + timedelta(minutes=30)))
    assert ts == [start]
