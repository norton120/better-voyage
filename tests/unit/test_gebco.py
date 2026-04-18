"""GEBCO bathymetry reader tests.

We don't ship a real GEBCO file in the repo — too big. Each test builds
a synthetic netCDF with known elevations, then exercises the reader.
The grid is 0.1° resolution over a 2°×2° window around the Chesapeake;
elevations are hand-picked so the bilinear lookup has clean expected
values.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from app.services.gebco import GebcoBathymetry, load_gebco_bbox


@pytest.fixture()
def synthetic_gebco(tmp_path: Path) -> Path:
    """Write a 21×21 synthetic GEBCO netCDF.

    The grid spans lat 37.0–39.0, lon -77.0 – -75.0 at 0.1° resolution.
    Elevation is a ramp: `-10 * (lat - 37)` metres — so lat=37 is the
    shoreline (0 m), lat=38 is -10 m depth, lat=39 is -20 m.
    """
    lats = np.linspace(37.0, 39.0, 21)
    lons = np.linspace(-77.0, -75.0, 21)
    elev = np.tile(-10.0 * (lats - 37.0)[:, None], (1, lons.size)).astype(np.float64)
    ds = xr.Dataset(
        {"elevation": (("lat", "lon"), elev)},
        coords={"lat": lats, "lon": lons},
    )
    path = tmp_path / "synthetic-gebco.nc"
    ds.to_netcdf(path)
    ds.close()
    return path


def test_load_and_depth_deep_water(synthetic_gebco: Path) -> None:
    gb = load_gebco_bbox(synthetic_gebco, bbox=(37.5, -76.5, 38.5, -75.5))
    # lat=38.5 → elevation = -10 * 1.5 = -15 → depth 15 m.
    depth = gb.depth(38.5, -76.0)
    assert depth is not None
    assert depth == pytest.approx(15.0, abs=1e-6)


def test_depth_bilinear_between_grid_cells(synthetic_gebco: Path) -> None:
    gb = load_gebco_bbox(synthetic_gebco, bbox=(37.5, -76.5, 38.5, -75.5))
    # lat=38.25 → elevation = -10 * 1.25 = -12.5 → depth 12.5.
    depth = gb.depth(38.25, -76.0)
    assert depth is not None
    assert depth == pytest.approx(12.5, abs=1e-6)


def test_depth_on_shoreline_returns_none(synthetic_gebco: Path) -> None:
    # lat=37.0 → elevation = 0 → treated as land / waterline.
    gb = load_gebco_bbox(synthetic_gebco, bbox=(36.5, -76.5, 37.5, -75.5))
    assert gb.depth(37.0, -76.0) is None


def test_depth_outside_loaded_bbox_returns_none(synthetic_gebco: Path) -> None:
    gb = load_gebco_bbox(synthetic_gebco, bbox=(37.5, -76.5, 38.5, -75.5))
    # Outside the requested bbox.
    assert gb.depth(37.0, -76.0) is None
    assert gb.depth(39.5, -76.0) is None
    assert gb.depth(38.0, -74.0) is None


def test_covers_bbox(synthetic_gebco: Path) -> None:
    gb = load_gebco_bbox(synthetic_gebco, bbox=(37.5, -76.5, 38.5, -75.5))
    assert gb.covers((37.6, -76.4, 38.4, -75.6)) is True
    assert gb.covers((37.4, -76.5, 38.5, -75.5)) is False  # lat_min below loaded


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_gebco_bbox(Path("/no/such/gebco.nc"), bbox=(0, 0, 1, 1))


def test_bbox_outside_grid_raises(synthetic_gebco: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        load_gebco_bbox(synthetic_gebco, bbox=(60.0, -170.0, 61.0, -169.0))


def test_unknown_variable_raises(tmp_path: Path) -> None:
    ds = xr.Dataset(
        {"nonsense": (("lat", "lon"), np.zeros((3, 3)))},
        coords={"lat": [0.0, 1.0, 2.0], "lon": [0.0, 1.0, 2.0]},
    )
    path = tmp_path / "bogus.nc"
    ds.to_netcdf(path)
    ds.close()
    with pytest.raises(ValueError, match="elevation"):
        load_gebco_bbox(path, bbox=(0, 0, 2, 2))


def test_coord_aliases_accepted(tmp_path: Path) -> None:
    # GEBCO historically used `latitude`/`longitude`; make sure we
    # tolerate that schema too.
    lats = np.linspace(10.0, 11.0, 11)
    lons = np.linspace(20.0, 21.0, 11)
    elev = np.full((11, 11), -50.0)
    ds = xr.Dataset(
        {"elevation": (("latitude", "longitude"), elev)},
        coords={"latitude": lats, "longitude": lons},
    )
    path = tmp_path / "alias-gebco.nc"
    ds.to_netcdf(path)
    ds.close()
    gb = load_gebco_bbox(path, bbox=(10.2, 20.2, 10.8, 20.8))
    assert gb.depth(10.5, 20.5) == pytest.approx(50.0, abs=1e-6)


def test_dataclass_shape() -> None:
    arr = xr.DataArray(
        np.full((3, 3), -10.0),
        coords={"lat": [0.0, 1.0, 2.0], "lon": [0.0, 1.0, 2.0]},
        dims=("lat", "lon"),
    )
    gb = GebcoBathymetry(elevation=arr, bbox=(0.0, 0.0, 2.0, 2.0))
    assert gb.depth(1.0, 1.0) == pytest.approx(10.0)
