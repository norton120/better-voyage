"""Planner silently snaps unnavigable endpoints to nearby water.

When an origin/destination lands on a land polygon or in water
shallower than the boat's `draft_m + min_depth_m`, the planner
spiral-searches outward (up to 2 nm) for the nearest navigable cell
and rewrites the request. Only when no snap exists inside 2 nm do we
surface `ENDPOINT_ON_LAND`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.schemas.request import Coord, TimeWindow, VoyageRequest
from app.services.charts import NullChartStore
from app.services.planner import PlannerError, _snap_endpoints_to_water


def _req(
    origin: tuple[float, float] = (38.9, -76.5),
    destination: tuple[float, float] = (38.5, -76.3),
) -> VoyageRequest:
    return VoyageRequest(
        origin=Coord(lat=origin[0], lon=origin[1], name="O"),
        destination=Coord(lat=destination[0], lon=destination[1], name="D"),
        window=TimeWindow(
            start_at=datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 4, 19, 0, 0, tzinfo=UTC),
            tz="UTC",
        ),
        boat_profile_name="default",
        objective="fastest",
        max_candidates=3,
    )


def test_endpoints_already_navigable_are_left_alone() -> None:
    req = _req()
    # NullChartStore → `inf` distance, 100 m depth: always navigable.
    _snap_endpoints_to_water(req, NullChartStore(), required_depth_m=2.3)
    assert (req.origin.lat, req.origin.lon) == (38.9, -76.5)
    assert (req.destination.lat, req.destination.lon) == (38.5, -76.3)


class _FixedBadPoints:
    """ChartStore stub: a fixed set of (lat, lon) keys are land/shoal,
    everything else is open 100 m water."""

    def __init__(self, bad: set[tuple[float, float]]) -> None:
        self._bad = bad

    def distance_to_land_nm(self, lat: float, lon: float) -> float:
        return 0.0 if (round(lat, 5), round(lon, 5)) in self._bad else 5.0

    def chart_depth(self, lat: float, lon: float) -> float | None:
        return 1.5 if (round(lat, 5), round(lon, 5)) in self._bad else 100.0


def test_origin_on_land_is_snapped() -> None:
    req = _req(origin=(38.9, -76.5))
    store = _FixedBadPoints(bad={(38.9, -76.5)})
    _snap_endpoints_to_water(req, store, required_depth_m=2.3)  # type: ignore[arg-type]
    # Original coord is bad; snapped coord must differ and be navigable.
    assert (req.origin.lat, req.origin.lon) != (38.9, -76.5)
    assert store.distance_to_land_nm(req.origin.lat, req.origin.lon) > 0.0


def test_destination_on_shoal_is_snapped() -> None:
    # Destination depth 1.5 m < required 2.3 m → counts as unnavigable
    # even though it's technically "in water".
    req = _req(destination=(38.5, -76.3))
    store = _FixedBadPoints(bad={(38.5, -76.3)})
    _snap_endpoints_to_water(req, store, required_depth_m=2.3)  # type: ignore[arg-type]
    assert (req.destination.lat, req.destination.lon) != (38.5, -76.3)
    depth = store.chart_depth(req.destination.lat, req.destination.lon)
    assert depth is not None and depth >= 2.3


class _AllBad:
    def distance_to_land_nm(self, lat: float, lon: float) -> float:
        return 0.0

    def chart_depth(self, lat: float, lon: float) -> float | None:
        return 0.0


def test_no_navigable_water_within_radius_raises() -> None:
    req = _req()
    with pytest.raises(PlannerError) as excinfo:
        _snap_endpoints_to_water(req, _AllBad(), required_depth_m=2.3)  # type: ignore[arg-type]
    assert excinfo.value.code == "ENDPOINT_ON_LAND"
    assert excinfo.value.stage == "charts_fetching"
    assert "origin" in (excinfo.value.detail or "")
