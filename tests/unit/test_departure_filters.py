"""Tests for local-time + night-arrival departure filtering."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from app.schemas.request import Coord, TimeWindow, VoyageRequest
from app.services.planner import _is_night_local, enumerate_departures


def _req_with_window(
    start: datetime,
    end: datetime,
    tz: str = "UTC",
    earliest: time | None = None,
    latest: time | None = None,
) -> VoyageRequest:
    return VoyageRequest(
        origin=Coord(lat=38.5, lon=-76.5),
        destination=Coord(lat=38.5, lon=-76.0),
        window=TimeWindow(
            start_at=start,
            end_at=end,
            tz=tz,
            earliest_departure_local_time=earliest,
            latest_departure_local_time=latest,
        ),
        boat_profile_name="default",
    )


def test_enumerate_no_filter_returns_hourly() -> None:
    ts = enumerate_departures(
        _req_with_window(
            datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 18, 3, 0, tzinfo=UTC),
        )
    )
    assert len(ts) == 4


def test_enumerate_filters_to_local_daylight_window() -> None:
    # 06:00-18:00 local (UTC here since tz="UTC").
    ts = enumerate_departures(
        _req_with_window(
            datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 18, 23, 0, tzinfo=UTC),
            earliest=time(6, 0),
            latest=time(18, 0),
        )
    )
    hours = {t.hour for t in ts}
    assert hours == set(range(6, 19))  # 6..18 inclusive


def test_enumerate_wrapping_midnight_window() -> None:
    # Night-time-only window: 22:00-04:00 local.
    ts = enumerate_departures(
        _req_with_window(
            datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 18, 23, 0, tzinfo=UTC),
            earliest=time(22, 0),
            latest=time(4, 0),
        )
    )
    hours = sorted({t.hour for t in ts})
    # 00, 01, 02, 03, 04, 22, 23
    assert hours == [0, 1, 2, 3, 4, 22, 23]


def test_enumerate_tz_shift_affects_filter() -> None:
    # 06:00-18:00 local America/New_York (UTC-4 on 2026-04-18, EDT).
    ts = enumerate_departures(
        _req_with_window(
            datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 18, 23, 0, tzinfo=UTC),
            tz="America/New_York",
            earliest=time(6, 0),
            latest=time(18, 0),
        )
    )
    # Local 06:00-18:00 EDT == UTC 10:00-22:00.
    utc_hours = sorted({t.hour for t in ts})
    assert utc_hours == list(range(10, 23))


def test_is_night_local_utc_window() -> None:
    tz = ZoneInfo("UTC")
    assert _is_night_local(datetime(2026, 4, 18, 23, 30, tzinfo=UTC), tz) is True
    assert _is_night_local(datetime(2026, 4, 18, 3, 0, tzinfo=UTC), tz) is True
    assert _is_night_local(datetime(2026, 4, 18, 12, 0, tzinfo=UTC), tz) is False
    assert _is_night_local(datetime(2026, 4, 18, 6, 0, tzinfo=UTC), tz) is False
