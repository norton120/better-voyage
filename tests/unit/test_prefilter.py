"""Pre-routing proxy prefilter keeps the best-looking departures.

Contract: `prefilter_departures` trims a list of enumerated departures
down to at most `max_keep`, ordered by the proxy score (lower is
better), preserving chronological order of the survivors. Missing
forecast samples score `inf` and drop to the bottom.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.services.forecast_field import ForecastField
from app.services.polars import DEFAULT_POLAR_PATH, Polar
from app.services.prefilter import prefilter_departures, proxy_score


def _uniform_field(
    *,
    lat_bounds: tuple[float, float],
    lon_bounds: tuple[float, float],
    start: datetime,
    hours: int,
    wind_kts: float,
    wind_from_deg: float,
    wave_h: float = 0.5,
) -> ForecastField:
    field = ForecastField(grid_res_deg=0.25)
    field.lat_grid = np.array([lat_bounds[0], lat_bounds[1]], dtype=float)
    field.lon_grid = np.array([lon_bounds[0], lon_bounds[1]], dtype=float)
    field.time_grid = np.array(
        [np.datetime64(f"{start.strftime('%Y-%m-%dT%H:%M:%S')}", "s")
         + np.timedelta64(h, "h")
         for h in range(hours)],
        dtype="datetime64[s]",
    )
    shape = (2, 2, hours)
    field.data = {
        "wind_speed_kts": np.full(shape, wind_kts),
        "wind_dir_deg": np.full(shape, wind_from_deg),
        "wind_gust_kts": np.full(shape, wind_kts * 1.25),
        "wave_height_m": np.full(shape, wave_h),
        "wave_period_s": np.full(shape, 4.0),
        "wave_dir_deg": np.full(shape, wind_from_deg),
        "current_speed_kts": np.full(shape, 0.0),
        "current_dir_deg": np.full(shape, 0.0),
    }
    return field


def test_prefilter_noop_when_under_cap() -> None:
    polar = Polar.load(DEFAULT_POLAR_PATH)
    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    field = _uniform_field(
        lat_bounds=(38.0, 39.0), lon_bounds=(-77.0, -75.0),
        start=depart, hours=24, wind_kts=12.0, wind_from_deg=270.0,
    )
    departures = [depart + timedelta(hours=h) for h in range(5)]
    kept = prefilter_departures(
        departures,
        origin=(38.5, -76.5),
        destination=(38.5, -75.5),
        polar=polar, forecast=field, max_keep=10,
    )
    assert kept == departures


def test_prefilter_trims_to_max_keep_and_stays_chronological() -> None:
    polar = Polar.load(DEFAULT_POLAR_PATH)
    depart = datetime(2026, 4, 18, 0, 0, tzinfo=UTC)
    field = _uniform_field(
        lat_bounds=(38.0, 39.0), lon_bounds=(-77.0, -75.0),
        start=depart, hours=24, wind_kts=12.0, wind_from_deg=270.0,
    )
    # Uniform wind → all 12 departures score equally; order by
    # insertion order then kept in chronological order.
    departures = [depart + timedelta(hours=h) for h in range(12)]
    kept = prefilter_departures(
        departures,
        origin=(38.5, -76.5),
        destination=(38.5, -75.5),
        polar=polar, forecast=field, max_keep=4,
    )
    assert len(kept) == 4
    # Chronological order preserved.
    assert kept == sorted(kept)


def test_prefilter_prefers_low_wave_departure_over_rough() -> None:
    """Two candidate times with identical wind but different waves —
    the calmer one should win."""
    polar = Polar.load(DEFAULT_POLAR_PATH)
    start = datetime(2026, 4, 18, 0, 0, tzinfo=UTC)

    # Build a forecast where waves are 4 m in the first half of the
    # window and 0.3 m in the second half — a cheap way to encode
    # "departure time matters for comfort."
    field = ForecastField(grid_res_deg=0.25)
    field.lat_grid = np.array([38.0, 39.0], dtype=float)
    field.lon_grid = np.array([-77.0, -75.0], dtype=float)
    hours = 24
    field.time_grid = np.array(
        [np.datetime64(f"{start.strftime('%Y-%m-%dT%H:%M:%S')}", "s")
         + np.timedelta64(h, "h")
         for h in range(hours)],
        dtype="datetime64[s]",
    )
    shape = (2, 2, hours)
    wave_arr = np.zeros(shape)
    wave_arr[..., : hours // 2] = 4.0
    wave_arr[..., hours // 2 :] = 0.3
    field.data = {
        "wind_speed_kts": np.full(shape, 12.0),
        "wind_dir_deg": np.full(shape, 270.0),
        "wind_gust_kts": np.full(shape, 15.0),
        "wave_height_m": wave_arr,
        "wave_period_s": np.full(shape, 4.0),
        "wave_dir_deg": np.full(shape, 270.0),
        "current_speed_kts": np.full(shape, 0.0),
        "current_dir_deg": np.full(shape, 0.0),
    }

    rough_depart = start + timedelta(hours=1)
    calm_depart = start + timedelta(hours=13)
    rough_score = proxy_score(
        depart_at=rough_depart,
        origin=(38.5, -76.5), destination=(38.5, -75.9),
        polar=polar, forecast=field,
    )
    calm_score = proxy_score(
        depart_at=calm_depart,
        origin=(38.5, -76.5), destination=(38.5, -75.9),
        polar=polar, forecast=field,
    )
    assert calm_score < rough_score


def test_prefilter_infinity_out_of_bbox_sinks_to_bottom() -> None:
    """Out-of-coverage sample → inf score → never chosen."""
    polar = Polar.load(DEFAULT_POLAR_PATH)
    start = datetime(2026, 4, 18, 0, 0, tzinfo=UTC)
    field = _uniform_field(
        lat_bounds=(38.0, 39.0), lon_bounds=(-77.0, -75.0),
        start=start, hours=24, wind_kts=12.0, wind_from_deg=270.0,
    )
    # Destination outside the forecast bbox → proxy_score returns inf.
    score = proxy_score(
        depart_at=start,
        origin=(38.5, -76.5), destination=(38.5, -70.0),
        polar=polar, forecast=field,
    )
    assert score == float("inf")
