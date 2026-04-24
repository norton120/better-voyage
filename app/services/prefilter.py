"""Pre-routing proxy scoring of candidate departures.

Serious tools route a curated subset of enumerated departures rather
than the full grid: a cheap forecast/polar proxy predicts which
departure windows *look* good, and the full isochrone only runs on
those. PredictWind's "best windows" calendar is the public face of
this pattern.

The proxy here:

1. Compute a nominal first-leg boat speed from wind at the origin at
   `depart_at` against the great-circle heading (one polar lookup).
2. Estimate passage duration as `distance / bsp_floor` using that BSP
   with a 2 kt floor against irons.
3. Re-sample wind + waves at origin, midpoint, destination at their
   respective "when you'd be there" times using the nominal duration.
4. Score = `passage_hours + α × discomfort_penalty` where discomfort
   sums `wave_height²` and an `over-20-kts` wind term.

Missing forecast samples (outside bbox or past horizon) score as
`inf` so the candidate sorts to the bottom.

This is explicitly *not* a replacement for isochrone routing — a
proxy-good candidate can still turn out to be a slog when tide and
tactical tacking enter the picture. It's a cheap pre-filter that
trades a handful of low-value runs for the ability to spend the full
compute budget on candidates that matter.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.forecast_field import ForecastField
from app.services.geo import bearing_deg, distance_nm, relative_wind_angle
from app.services.polars import Polar

_BSP_FLOOR_KTS = 2.0
_DISCOMFORT_WEIGHT = 0.3


def proxy_score(
    *,
    depart_at: datetime,
    origin: tuple[float, float],
    destination: tuple[float, float],
    polar: Polar,
    forecast: ForecastField,
) -> float:
    """Cheap scalar predictor for how good this departure looks.

    Lower is better. Returns `inf` if the forecast can't cover the
    sampled (lat, lon, t) points.
    """
    gc = bearing_deg(origin[0], origin[1], destination[0], destination[1])
    total_nm = distance_nm(origin[0], origin[1], destination[0], destination[1])
    if total_nm <= 0:
        return 0.0

    env0 = forecast.at(origin[0], origin[1], depart_at)
    if env0 is None:
        return float("inf")
    twa0 = relative_wind_angle(gc, env0.wind_dir_deg)
    bsp0 = max(polar.bsp(twa0, env0.wind_speed_kts), _BSP_FLOOR_KTS)
    nominal_h = total_nm / bsp0

    mid_lat = 0.5 * (origin[0] + destination[0])
    mid_lon = 0.5 * (origin[1] + destination[1])
    t_mid = depart_at + timedelta(hours=nominal_h / 2)
    t_end = depart_at + timedelta(hours=nominal_h)

    env_mid = forecast.at(mid_lat, mid_lon, t_mid)
    env_end = forecast.at(destination[0], destination[1], t_end)
    if env_mid is None or env_end is None:
        return float("inf")

    twa_mid = relative_wind_angle(gc, env_mid.wind_dir_deg)
    twa_end = relative_wind_angle(gc, env_end.wind_dir_deg)
    bsp_mid = max(polar.bsp(twa_mid, env_mid.wind_speed_kts), _BSP_FLOOR_KTS)
    bsp_end = max(polar.bsp(twa_end, env_end.wind_speed_kts), _BSP_FLOOR_KTS)

    avg_bsp = (bsp0 + bsp_mid + bsp_end) / 3.0
    refined_eta_h = total_nm / max(avg_bsp, _BSP_FLOOR_KTS)

    discomfort = 0.0
    for env in (env0, env_mid, env_end):
        discomfort += env.wave_height_m ** 2
        if env.wind_speed_kts > 20.0:
            discomfort += 0.1 * (env.wind_speed_kts - 20.0)

    return refined_eta_h + _DISCOMFORT_WEIGHT * discomfort


def prefilter_departures(
    departures: list[datetime],
    *,
    origin: tuple[float, float],
    destination: tuple[float, float],
    polar: Polar,
    forecast: ForecastField,
    max_keep: int,
) -> list[datetime]:
    """Score and trim to the best `max_keep` departures.

    Returns the surviving departures in chronological order (not score
    order) so downstream code doesn't need to know the list was
    reordered. With fewer than `max_keep` departures in, returns the
    input unchanged.
    """
    if max_keep <= 0 or len(departures) <= max_keep:
        return list(departures)
    scored: list[tuple[float, datetime]] = [
        (
            proxy_score(
                depart_at=d,
                origin=origin,
                destination=destination,
                polar=polar,
                forecast=forecast,
            ),
            d,
        )
        for d in departures
    ]
    scored.sort(key=lambda x: x[0])
    survivors = [d for _, d in scored[:max_keep]]
    survivors.sort()
    return survivors
