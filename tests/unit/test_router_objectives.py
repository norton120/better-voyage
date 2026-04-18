"""Router objective tests — comfortable vs fastest vs short_tacks."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from app.services.charts import NullChartStore
from app.services.forecast_field import ForecastField
from app.services.polars import DEFAULT_POLAR_PATH, Polar
from app.services.router import BoatLimits, plan_candidate


def _field_with_rough_northern_half(
    depart: datetime,
) -> ForecastField:
    """Forecast grid: wind uniform from south at 12 kt, but wave_height
    is 0.2 m on the southern row and 2.0 m on the northern row.

    A comfortable route should hug the south; a fastest route
    (direct east) is geographically between the two bands.
    """
    field = ForecastField(grid_res_deg=0.25)
    # 3 latitudes: 38.25, 38.5, 38.75 ; 4 longitudes.
    field.lat_grid = np.array([38.25, 38.5, 38.75], dtype=float)
    field.lon_grid = np.array([-76.6, -76.4, -76.2, -76.0], dtype=float)
    n_hours = 12
    field.time_grid = np.array(
        [np.datetime64(depart.strftime("%Y-%m-%dT%H:%M:%S"), "s") + np.timedelta64(h, "h")
         for h in range(n_hours)],
        dtype="datetime64[s]",
    )
    shape = (3, 4, n_hours)
    waves = np.zeros(shape)
    waves[0, :, :] = 0.2     # calm south
    waves[1, :, :] = 1.0     # moderate middle
    waves[2, :, :] = 2.0     # rough north
    field.data = {
        "wind_speed_kts": np.full(shape, 12.0),
        "wind_dir_deg": np.full(shape, 180.0),   # wind from south
        "wind_gust_kts": np.full(shape, 15.0),
        "wave_height_m": waves,
        "wave_period_s": np.full(shape, 4.0),
        "wave_dir_deg": np.full(shape, 180.0),
        "current_speed_kts": np.full(shape, 0.0),
        "current_dir_deg": np.full(shape, 0.0),
    }
    return field


def _mean_wave(points) -> float:
    vals = [p.env.wave_height_m for p in points if p.env is not None]
    return float(sum(vals) / len(vals)) if vals else 0.0


def test_comfortable_picks_calmer_path_than_fastest() -> None:
    polar = Polar.load(DEFAULT_POLAR_PATH)
    charts = NullChartStore()
    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    field = _field_with_rough_northern_half(depart)
    origin = (38.5, -76.55)
    destination = (38.5, -76.05)

    fast = plan_candidate(
        origin=origin, destination=destination, depart_at=depart,
        polar=polar, forecast=field, charts=charts, boat=BoatLimits(),
        objective="fastest",
        step_minutes=30, max_steps=24, arrival_tolerance_nm=1.0,
    )
    comfy = plan_candidate(
        origin=origin, destination=destination, depart_at=depart,
        polar=polar, forecast=field, charts=charts, boat=BoatLimits(),
        objective="comfortable",
        step_minutes=30, max_steps=24, arrival_tolerance_nm=1.0,
    )
    # Both should arrive; both are valid routes.
    assert len(fast.points) >= 2
    assert len(comfy.points) >= 2
    # Comfortable should sample calmer waves on average.
    assert _mean_wave(comfy.points) <= _mean_wave(fast.points)


def test_short_tacks_objective_returns_route() -> None:
    """`short_tacks` plumbs through without breaking routing."""
    polar = Polar.load(DEFAULT_POLAR_PATH)
    charts = NullChartStore()
    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    field = _field_with_rough_northern_half(depart)
    result = plan_candidate(
        origin=(38.5, -76.55),
        destination=(38.5, -76.05),
        depart_at=depart,
        polar=polar, forecast=field, charts=charts, boat=BoatLimits(),
        objective="short_tacks",
        step_minutes=30, max_steps=24, arrival_tolerance_nm=1.0,
    )
    assert len(result.points) >= 2
    assert result.objective == "short_tacks"
