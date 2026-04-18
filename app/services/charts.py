"""ChartStore interface + a deliberately-empty stand-in.

The real implementation — NOAA ENC, OpenSeaMap, GEBCO — lands with the
heavy geospatial dependencies later in M2 (plan/15-charts-bathymetry).
Until then, `NullChartStore` satisfies the interface so the router can
be exercised in isolation.

Using `NullChartStore` in production would violate the
"we either know the water or we don't plan" rule in plan/15. It's
explicitly labelled as a development-only stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

Bbox = tuple[float, float, float, float]  # lat_min, lon_min, lat_max, lon_max


@dataclass(frozen=True)
class ChartCoverage:
    enc_cells: int = 0
    osm_extracts: int = 0
    gebco_tile: str | None = None
    gaps: list[Bbox] | None = None


@dataclass(frozen=True)
class Waypoint:
    lat: float
    lon: float
    name: str | None = None
    sym: str | None = None
    desc: str | None = None


class ChartStore(Protocol):
    async def coverage(self, bbox: Bbox) -> ChartCoverage: ...
    async def ensure_coverage(self, bbox: Bbox) -> None: ...
    def crosses_land(self, a: tuple[float, float], b: tuple[float, float]) -> bool: ...
    def crosses_obstacle(self, a: tuple[float, float], b: tuple[float, float]) -> bool: ...
    def is_restricted(self, pt: tuple[float, float]) -> bool: ...
    def chart_depth(self, lat: float, lon: float) -> float | None: ...
    def available_depth(self, lat: float, lon: float, t: datetime) -> float | None: ...
    def navaids_in(self, bbox: Bbox) -> list[Waypoint]: ...


class NullChartStore:
    """Development stub — treats the entire planet as navigable water.

    DO NOT use in production. Real voyage planning REQUIRES the real
    ChartStore per plan/15; the router refuses to plan without chart
    coverage. This class exists so the router + planner can be unit-
    tested before the ENC / OSM / GEBCO ingest lands.
    """

    async def coverage(self, bbox: Bbox) -> ChartCoverage:
        return ChartCoverage(
            enc_cells=0, osm_extracts=0, gebco_tile=None, gaps=[]
        )

    async def ensure_coverage(self, bbox: Bbox) -> None:
        return None

    def crosses_land(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        return False

    def crosses_obstacle(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        return False

    def is_restricted(self, pt: tuple[float, float]) -> bool:
        return False

    def chart_depth(self, lat: float, lon: float) -> float | None:
        return 100.0

    def available_depth(self, lat: float, lon: float, t: datetime) -> float | None:
        return 100.0

    def navaids_in(self, bbox: Bbox) -> list[Waypoint]:
        return []
