"""WGS84 geodesic helpers.

Thin layer over `pyproj.Geod` with the units the rest of the planner
speaks: **nautical miles** for distance, **degrees** for bearings.
"""

from __future__ import annotations

from pyproj import Geod

_GEOD = Geod(ellps="WGS84")

METERS_PER_NM = 1852.0


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    _, _, meters = _GEOD.inv(lon1, lat1, lon2, lat2)
    return meters / METERS_PER_NM


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    az, _, _ = _GEOD.inv(lon1, lat1, lon2, lat2)
    return az % 360.0


def advance(lat: float, lon: float, bearing_deg: float, distance_nm: float) -> tuple[float, float]:
    """Move `distance_nm` along `bearing_deg` from (lat, lon). Returns (lat, lon)."""
    lon2, lat2, _ = _GEOD.fwd(lon, lat, bearing_deg, distance_nm * METERS_PER_NM)
    return lat2, lon2


def relative_wind_angle(course_deg: float, wind_from_deg: float) -> float:
    """True-wind angle relative to a course.

    Wind direction is conventionally "from" (met convention). Returns
    the angle between the wind source and the boat's heading: 0 =
    dead into the wind, 180 = dead downwind, 90 = beam. Always in
    [0, 180].
    """
    rel = (wind_from_deg - course_deg) % 360.0
    if rel > 180.0:
        rel = 360.0 - rel
    return rel
