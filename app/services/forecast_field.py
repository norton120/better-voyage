"""In-memory forecast grid with bilinear-spatial + linear-temporal interpolation.

Usage:

    field = ForecastField()
    await field.prefetch(bbox=(lat_min, lon_min, lat_max, lon_max),
                         start=t0, end=t1)
    env = field.at(lat, lon, t)   # -> Env or None outside coverage

The grid is built by sampling Open-Meteo Marine + Forecast point queries
(plan/03 §Sampling & prefetch). Concurrency is bounded by
`BV_MAX_CONCURRENT_FETCHES`. Every point call flows through the existing
SQLite cache (plan/11), so re-running a voyage with the same bbox is a
cache hit.

Internal units: **knots** for speeds, **meters** for heights, **seconds**
for periods, **degrees (from)** for directions. Open-Meteo returns km/h
for wind / current speeds by default; we convert at grid-build time so
downstream code never has to think about it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

from app.clients import open_meteo
from app.clients.cache import CacheResult
from app.config import get_settings
from app.logging import get_logger
from app.observability import meter, tracer

log = get_logger(__name__)
_tracer = tracer("app.services.forecast_field")
_prefetch_points = meter("app.services.forecast_field").create_counter(
    "bv.forecast.prefetch_points", unit="1"
)

Bbox = tuple[float, float, float, float]  # lat_min, lon_min, lat_max, lon_max

KMH_TO_KTS = 1 / 1.852


@dataclass(frozen=True)
class Env:
    """Interpolated environment at (lat, lon, t)."""

    wind_speed_kts: float
    wind_dir_deg: float
    wind_gust_kts: float
    wave_height_m: float
    wave_period_s: float
    wave_dir_deg: float
    current_speed_kts: float
    current_dir_deg: float


class ForecastField:
    """A bbox-by-time-window forecast cube with trilinear sampling."""

    def __init__(self, grid_res_deg: float = 0.25) -> None:
        self.grid_res_deg = grid_res_deg
        self.lat_grid: np.ndarray | None = None
        self.lon_grid: np.ndarray | None = None
        self.time_grid: np.ndarray | None = None  # dtype=datetime64[s], tz-naive UTC
        self.data: dict[str, np.ndarray] = {}
        # Oldest `fetched_at` among any upstream row that served stale
        # during `prefetch`. `None` means every upstream fetch was fresh.
        # Callers propagate this into `voyages.coverage_json` so the user
        # sees when the forecast last came from the live API.
        self.stale_at: datetime | None = None

    # ---- prefetch ----------------------------------------------------

    async def prefetch(self, bbox: Bbox, start: datetime, end: datetime) -> None:
        """Populate the grid by issuing one point fetch per (lat, lon).

        `start` / `end` are inclusive; fetches extend the day range so
        hourly samples bracket the requested window.
        """
        lat_min, lon_min, lat_max, lon_max = bbox
        self.lat_grid = _axis(lat_min, lat_max, self.grid_res_deg)
        self.lon_grid = _axis(lon_min, lon_max, self.grid_res_deg)

        start_d: date = start.date()
        end_d: date = end.date()

        settings = get_settings()
        sem = asyncio.Semaphore(settings.http_retries * 0 + 4)  # BV_MAX_CONCURRENT_FETCHES default 4

        grid_points: list[tuple[int, int, float, float]] = []
        for i, la in enumerate(self.lat_grid):
            for j, lo in enumerate(self.lon_grid):
                grid_points.append((i, j, float(la), float(lo)))

        with _tracer.start_as_current_span(
            "forecast.prefetch",
            attributes={
                "bbox.lat_min": lat_min, "bbox.lon_min": lon_min,
                "bbox.lat_max": lat_max, "bbox.lon_max": lon_max,
                "grid.points": len(grid_points),
                "window.start": start.isoformat(),
                "window.end": end.isoformat(),
            },
        ):
            async def _one(
                i: int, j: int, la: float, lo: float
            ) -> tuple[int, int, CacheResult, CacheResult]:
                async with sem:
                    marine = await open_meteo.fetch_marine(la, lo, start_d, end_d)
                    wind = await open_meteo.fetch_wind(la, lo, start_d, end_d)
                _prefetch_points.add(1)
                return i, j, marine, wind

            fetched = await asyncio.gather(
                *[_one(i, j, la, lo) for (i, j, la, lo) in grid_points]
            )

        stale_candidates: list[datetime] = []
        for _, _, marine_cr, wind_cr in fetched:
            if marine_cr.stale:
                stale_candidates.append(marine_cr.fetched_at)
            if wind_cr.stale:
                stale_candidates.append(wind_cr.fetched_at)
        if stale_candidates:
            self.stale_at = min(stale_candidates)

        results: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = [
            (i, j, m.body, w.body) for (i, j, m, w) in fetched
        ]

        # Time axis from the first non-empty response.
        sample_hours: list[str] = []
        for _, _, marine, _ in results:
            if marine.get("hourly", {}).get("time"):
                sample_hours = marine["hourly"]["time"]
                break
        if not sample_hours:
            raise RuntimeError("no forecast data returned — nothing to interpolate")

        self.time_grid = np.array(
            [np.datetime64(h, "s") for h in sample_hours], dtype="datetime64[s]"
        )

        shape = (self.lat_grid.size, self.lon_grid.size, self.time_grid.size)
        empty = np.full(shape, np.nan, dtype=float)
        arrays = {
            "wind_speed_kts": empty.copy(),
            "wind_dir_deg": empty.copy(),
            "wind_gust_kts": empty.copy(),
            "wave_height_m": empty.copy(),
            "wave_period_s": empty.copy(),
            "wave_dir_deg": empty.copy(),
            "current_speed_kts": empty.copy(),
            "current_dir_deg": empty.copy(),
        }

        for i, j, marine, wind in results:
            mh = marine.get("hourly", {})
            wh = wind.get("hourly", {})
            _fill(arrays["wave_height_m"], i, j, mh.get("wave_height"))
            _fill(arrays["wave_period_s"], i, j, mh.get("wave_period"))
            _fill(arrays["wave_dir_deg"], i, j, mh.get("wave_direction"))
            _fill_convert(arrays["current_speed_kts"], i, j, mh.get("ocean_current_velocity"), KMH_TO_KTS)
            _fill(arrays["current_dir_deg"], i, j, mh.get("ocean_current_direction"))
            _fill_convert(arrays["wind_speed_kts"], i, j, wh.get("wind_speed_10m"), KMH_TO_KTS)
            _fill_convert(arrays["wind_gust_kts"], i, j, wh.get("wind_gusts_10m"), KMH_TO_KTS)
            _fill(arrays["wind_dir_deg"], i, j, wh.get("wind_direction_10m"))

        self.data = arrays
        log.info(
            "forecast.prefetch.done",
            grid_ny=int(self.lat_grid.size),
            grid_nx=int(self.lon_grid.size),
            grid_nt=int(self.time_grid.size),
        )

    # ---- interpolation -----------------------------------------------

    def at(self, lat: float, lon: float, t: datetime) -> Env | None:
        if self.lat_grid is None or self.lon_grid is None or self.time_grid is None:
            return None

        if not (self.lat_grid[0] <= lat <= self.lat_grid[-1]):
            return None
        if not (self.lon_grid[0] <= lon <= self.lon_grid[-1]):
            return None

        tt = np.datetime64(t.astimezone(UTC).replace(tzinfo=None), "s")
        if not (self.time_grid[0] <= tt <= self.time_grid[-1]):
            return None

        i, fi = _index_and_frac(self.lat_grid, lat)
        j, fj = _index_and_frac(self.lon_grid, lon)
        k, fk = _index_and_frac_time(self.time_grid, tt)

        def sample(arr: np.ndarray) -> float:
            corners = np.array(
                [
                    arr[i, j, k], arr[i, j, k + 1],
                    arr[i, j + 1, k], arr[i, j + 1, k + 1],
                    arr[i + 1, j, k], arr[i + 1, j, k + 1],
                    arr[i + 1, j + 1, k], arr[i + 1, j + 1, k + 1],
                ]
            )
            if np.isnan(corners).any():
                return float("nan")
            weights = np.array(
                [
                    (1 - fi) * (1 - fj) * (1 - fk),
                    (1 - fi) * (1 - fj) * fk,
                    (1 - fi) * fj * (1 - fk),
                    (1 - fi) * fj * fk,
                    fi * (1 - fj) * (1 - fk),
                    fi * (1 - fj) * fk,
                    fi * fj * (1 - fk),
                    fi * fj * fk,
                ]
            )
            return float((corners * weights).sum())

        values = {k2: sample(v) for k2, v in self.data.items()}
        if any(np.isnan(v) for v in values.values()):
            return None
        return Env(**values)


# --- module-level helpers ---------------------------------------------------


def _axis(lo: float, hi: float, res: float) -> np.ndarray:
    n = max(2, round((hi - lo) / res) + 1)
    return np.linspace(lo, hi, n)


def _fill(arr: np.ndarray, i: int, j: int, series: list[float] | None) -> None:
    if not series:
        return
    vals = np.asarray(series, dtype=float)
    n = min(vals.size, arr.shape[2])
    arr[i, j, :n] = vals[:n]


def _fill_convert(arr: np.ndarray, i: int, j: int, series: list[float] | None, factor: float) -> None:
    if not series:
        return
    vals = np.asarray(series, dtype=float) * factor
    n = min(vals.size, arr.shape[2])
    arr[i, j, :n] = vals[:n]


def _index_and_frac(axis: np.ndarray, x: float) -> tuple[int, float]:
    i = int(np.searchsorted(axis, x, side="right") - 1)
    i = max(0, min(i, axis.size - 2))
    a, b = float(axis[i]), float(axis[i + 1])
    f = (x - a) / (b - a) if b > a else 0.0
    return i, f


def _index_and_frac_time(axis: np.ndarray, x: np.datetime64) -> tuple[int, float]:
    i = int(np.searchsorted(axis, x, side="right") - 1)
    i = max(0, min(i, axis.size - 2))
    a = axis[i].astype("int64")
    b = axis[i + 1].astype("int64")
    xi = x.astype("int64")
    f = (xi - a) / (b - a) if b > a else 0.0
    return i, float(f)
