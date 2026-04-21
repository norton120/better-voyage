"""Planner refuses to route on-land endpoints at charts_fetching.

Issue 01: before this check, picking a point inside a land polygon
would run charts_fetching → forecast_prefetching → every candidate
route and only fail in the router as `ROUTE_BLOCKED`. The validation
runs right after `ensure_coverage` and surfaces a precise error with
the offending endpoint's label + coordinates.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.schemas.request import Coord, TimeWindow, VoyageRequest
from app.services.charts import NullChartStore
from app.services.planner import PlannerError, _validate_endpoints_in_water


def _req() -> VoyageRequest:
    return VoyageRequest(
        origin=Coord(lat=38.9, lon=-76.5, name="O"),
        destination=Coord(lat=38.5, lon=-76.3, name="D"),
        window=TimeWindow(
            start_at=datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 4, 19, 0, 0, tzinfo=UTC),
            tz="UTC",
        ),
        boat_profile_name="default",
        objective="fastest",
        max_candidates=3,
    )


def test_both_endpoints_in_water_passes() -> None:
    # NullChartStore reports `inf` distance everywhere — classic water.
    _validate_endpoints_in_water(_req(), NullChartStore())


def test_origin_on_land_raises_with_label() -> None:
    class _OriginOnLand:
        def distance_to_land_nm(self, lat: float, lon: float) -> float:
            return 0.0 if (lat, lon) == (38.9, -76.5) else 5.0

    with pytest.raises(PlannerError) as excinfo:
        _validate_endpoints_in_water(_req(), _OriginOnLand())  # type: ignore[arg-type]
    assert excinfo.value.code == "ENDPOINT_ON_LAND"
    assert excinfo.value.stage == "charts_fetching"
    assert "origin" in (excinfo.value.detail or "")


def test_destination_on_land_raises_with_label() -> None:
    class _DestOnLand:
        def distance_to_land_nm(self, lat: float, lon: float) -> float:
            return 0.0 if (lat, lon) == (38.5, -76.3) else 5.0

    with pytest.raises(PlannerError) as excinfo:
        _validate_endpoints_in_water(_req(), _DestOnLand())  # type: ignore[arg-type]
    assert excinfo.value.code == "ENDPOINT_ON_LAND"
    assert "destination" in (excinfo.value.detail or "")
