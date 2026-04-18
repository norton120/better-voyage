"""Geodesic helper tests."""

from __future__ import annotations

import pytest

from app.services.geo import (
    advance,
    bearing_deg,
    distance_nm,
    relative_wind_angle,
)

# Annapolis (38.9784, -76.4922) → Norfolk (36.8467, -76.2929)
ANNAPOLIS = (38.9784, -76.4922)
NORFOLK = (36.8467, -76.2929)


def test_distance_annapolis_to_norfolk() -> None:
    d = distance_nm(*ANNAPOLIS, *NORFOLK)
    # Great-circle ~128 nm. Chesapeake land-path aside, this is just a
    # pure geodesic check.
    assert 127.0 < d < 130.0


def test_bearing_annapolis_to_norfolk_is_south() -> None:
    b = bearing_deg(*ANNAPOLIS, *NORFOLK)
    # Mostly southward, slight eastward lean.
    assert 170 < b < 180


def test_advance_round_trip_returns_origin() -> None:
    # advance N nm, then back along the return geodesic bearing → origin.
    b = bearing_deg(*ANNAPOLIS, *NORFOLK)
    lat2, lon2 = advance(*ANNAPOLIS, b, 50.0)
    # On an ellipsoid the return bearing != (b + 180), so query it.
    back_b = bearing_deg(lat2, lon2, *ANNAPOLIS)
    lat3, lon3 = advance(lat2, lon2, back_b, 50.0)
    assert lat3 == pytest.approx(ANNAPOLIS[0], abs=1e-6)
    assert lon3 == pytest.approx(ANNAPOLIS[1], abs=1e-6)


def test_relative_wind_angle_symmetric() -> None:
    assert relative_wind_angle(0, 0) == 0
    assert relative_wind_angle(0, 90) == 90
    assert relative_wind_angle(0, 180) == 180
    # Wind from behind = 180 regardless of course.
    assert relative_wind_angle(90, 270) == 180
    # Mirror symmetry: port vs starboard 10° off is the same magnitude.
    assert relative_wind_angle(0, 10) == relative_wind_angle(0, 350)
