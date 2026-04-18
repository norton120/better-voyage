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

    The hot `depth(lat, lon)` path does a direct numpy bilinear
    interpolation — `xarray.interp` is ~100x slower per call because
    of coordinate alignment overhead, and the router calls this
    millions of times per voyage.
    """

    elevation: xr.DataArray
    bbox: Bbox

    def __post_init__(self) -> None:
        # Snapshot grid coordinates as plain numpy arrays for the hot
        # path. Using an increasing sort lets `searchsorted` locate
        # bracketing cells in O(log n) without xarray's per-call setup.
        arr = self.elevation
        lats = arr.coords["lat"].values.astype(np.float64, copy=False)
        lons = arr.coords["lon"].values.astype(np.float64, copy=False)
        vals = arr.values.astype(np.float64, copy=False)
        if lats.size > 1 and lats[1] < lats[0]:
            lats = lats[::-1]
            vals = vals[::-1, :]
        if lons.size > 1 and lons[1] < lons[0]:
            lons = lons[::-1]
            vals = vals[:, ::-1]
        # Stash on private attrs — dataclass(frozen=False) so assignment
        # works; leave the xarray view alone for anyone who wants it.
        object.__setattr__(self, "_lats", lats)
        object.__setattr__(self, "_lons", lons)
        object.__setattr__(self, "_vals", vals)

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
        lats = self._lats
        lons = self._lons
        vals = self._vals
        # Locate bracketing cell. searchsorted returns the insertion
        # index — j means lats[j-1] <= lat <= lats[j].
        j = int(np.searchsorted(lats, lat))
        i = int(np.searchsorted(lons, lon))
        if j == 0:
            j = 1
        elif j >= lats.size:
            j = lats.size - 1
        if i == 0:
            i = 1
        elif i >= lons.size:
            i = lons.size - 1
        lat0, lat1 = lats[j - 1], lats[j]
        lon0, lon1 = lons[i - 1], lons[i]
        v00 = vals[j - 1, i - 1]
        v01 = vals[j - 1, i]
        v10 = vals[j, i - 1]
        v11 = vals[j, i]
        if np.isnan(v00) or np.isnan(v01) or np.isnan(v10) or np.isnan(v11):
            return None
        tlat = 0.0 if lat1 == lat0 else (lat - lat0) / (lat1 - lat0)
        tlon = 0.0 if lon1 == lon0 else (lon - lon0) / (lon1 - lon0)
        a = v00 * (1 - tlon) + v01 * tlon
        b = v10 * (1 - tlon) + v11 * tlon
        elevation_m = a * (1 - tlat) + b * tlat
        if elevation_m >= 0.0:
            return None
        return float(-elevation_m)


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
