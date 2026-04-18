"""GEBCO bathymetry reader.

GEBCO publishes a 15-arc-second (~450 m) global grid of elevation values
as netCDF. Values are meters relative to the geoid: negative = below sea
level, positive = land. This module exposes a bbox-scoped slice of the
grid with a bilinear depth lookup.

Usage:

    gb = load_gebco_bbox(path, bbox=(lat_min, lon_min, lat_max, lon_max))
    depth_m = gb.depth(lat, lon)   # None over land or outside the slice

The load-by-bbox shape matches `ChartStore.ensure_coverage` (plan/03
Part 2): we never materialize the full global grid in RAM. Land / no-
coverage returns `None`; the ChartStore is expected to treat `None` as
"don't go there."
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xarray as xr

from app.observability import meter, tracer

Bbox = tuple[float, float, float, float]  # lat_min, lon_min, lat_max, lon_max


_tracer = tracer("app.services.gebco")
_m = meter("app.services.gebco")
_cells_loaded = _m.create_counter(
    "bv.charts.cells_loaded",
    description="GEBCO bathymetry tiles loaded into memory",
    unit="1",
)


@dataclass
class GebcoBathymetry:
    """A bbox-local bathymetry slice backed by a GEBCO netCDF.

    `elevation` is an xarray DataArray with (`lat`, `lon`) dims and
    meters-above-geoid values. The slice covers `bbox` inclusive; points
    outside return `None` from `depth()`.
    """

    elevation: xr.DataArray
    bbox: Bbox

    def covers(self, bbox: Bbox) -> bool:
        """True iff `bbox` is fully inside the loaded slice."""
        lat_min, lon_min, lat_max, lon_max = bbox
        s_lat_min, s_lon_min, s_lat_max, s_lon_max = self.bbox
        return (
            s_lat_min <= lat_min
            and s_lon_min <= lon_min
            and s_lat_max >= lat_max
            and s_lon_max >= lon_max
        )

    def depth(self, lat: float, lon: float) -> float | None:
        """Bilinear-interpolated depth in meters, or None off-grid / on land.

        `None` means either (a) lat/lon is outside the loaded slice or
        (b) the interpolated elevation is >= 0 (land or waterline).
        """
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            return None
        # xarray's .interp with method="linear" does the bilinear math.
        # We drop the coordinate scalars and read the value.
        val = self.elevation.interp(
            lat=lat, lon=lon, method="linear"
        ).values
        if np.isnan(val):
            return None
        elevation_m = float(val)
        if elevation_m >= 0.0:
            return None
        return -elevation_m


def load_gebco_bbox(path: Path, bbox: Bbox) -> GebcoBathymetry:
    """Open a GEBCO netCDF, slice to `bbox`, return an in-memory bathymetry.

    `bbox` is `(lat_min, lon_min, lat_max, lon_max)`. The returned slice
    is padded one grid cell in each direction so that bilinear
    interpolation at the bbox edges stays well-defined.

    Raises `FileNotFoundError` if `path` does not exist; `ValueError` if
    the dataset has no recognizable elevation variable or the bbox lies
    outside the grid.
    """
    if not path.exists():
        raise FileNotFoundError(f"GEBCO file not found: {path}")

    with _tracer.start_as_current_span(
        "charts.load",
        attributes={"charts.source": "gebco", "charts.path": str(path)},
    ) as span:
        ds = xr.open_dataset(path)
        try:
            var_name = _find_elevation_var(ds)
            lat_name, lon_name = _find_coord_names(ds)
            arr = ds[var_name]

            lat_min, lon_min, lat_max, lon_max = bbox
            lat_coord = ds[lat_name]
            lon_coord = ds[lon_name]
            lat_step = abs(float(lat_coord[1] - lat_coord[0])) if lat_coord.size > 1 else 0.0
            lon_step = abs(float(lon_coord[1] - lon_coord[0])) if lon_coord.size > 1 else 0.0
            pad_lat = lat_step
            pad_lon = lon_step

            arr = arr.sel(
                {
                    lat_name: slice(lat_min - pad_lat, lat_max + pad_lat),
                    lon_name: slice(lon_min - pad_lon, lon_max + pad_lon),
                }
            )
            if arr.sizes.get(lat_name, 0) == 0 or arr.sizes.get(lon_name, 0) == 0:
                raise ValueError(
                    f"GEBCO bbox {bbox} produced empty slice — likely outside the grid"
                )
            # Materialize into memory (xarray lazy-loads by default).
            arr = arr.load()
            # Normalize coord names to lat/lon for downstream `depth()`.
            arr = arr.rename({lat_name: "lat", lon_name: "lon"})
            span.set_attribute("charts.cells", int(arr.size))
            _cells_loaded.add(1, {"source": "gebco"})
            return GebcoBathymetry(elevation=arr, bbox=bbox)
        finally:
            ds.close()


_ELEVATION_VARS = ("elevation", "z", "bathymetry", "depth")
_LAT_NAMES = ("lat", "latitude", "y")
_LON_NAMES = ("lon", "longitude", "x")


def _find_elevation_var(ds: xr.Dataset) -> str:
    for name in _ELEVATION_VARS:
        if name in ds.data_vars:
            return name
    raise ValueError(
        f"GEBCO dataset has none of {_ELEVATION_VARS}; got {list(ds.data_vars)}"
    )


def _find_coord_names(ds: xr.Dataset) -> tuple[str, str]:
    lat = next((n for n in _LAT_NAMES if n in ds.coords), None)
    lon = next((n for n in _LON_NAMES if n in ds.coords), None)
    if lat is None or lon is None:
        raise ValueError(
            f"GEBCO dataset missing lat/lon coords; got {list(ds.coords)}"
        )
    return lat, lon


__all__ = ["Bbox", "GebcoBathymetry", "load_gebco_bbox"]
