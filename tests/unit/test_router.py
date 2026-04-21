"""Isochrone router — smoke test over a uniform forecast field."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from app.services.charts import NullChartStore
from app.services.forecast_field import ForecastField
from app.services.polars import DEFAULT_POLAR_PATH, Polar
from app.services.router import BoatLimits, RouterError, plan_candidate, sector_prune


def _uniform_field(
    *,
    lat_bounds: tuple[float, float],
    lon_bounds: tuple[float, float],
    start: datetime,
    hours: int,
    wind_kts: float,
    wind_from_deg: float,
) -> ForecastField:
    field = ForecastField(grid_res_deg=0.25)
    field.lat_grid = np.array([lat_bounds[0], lat_bounds[1]], dtype=float)
    field.lon_grid = np.array([lon_bounds[0], lon_bounds[1]], dtype=float)
    field.time_grid = np.array(
        [np.datetime64(f"{start.strftime('%Y-%m-%dT%H:%M:%S')}", "s")
         + np.timedelta64(h, "h")
         for h in range(hours)],
        dtype="datetime64[s]",
    )
    shape = (2, 2, hours)
    field.data = {
        "wind_speed_kts": np.full(shape, wind_kts),
        "wind_dir_deg": np.full(shape, wind_from_deg),
        "wind_gust_kts": np.full(shape, wind_kts * 1.25),
        "wave_height_m": np.full(shape, 0.5),
        "wave_period_s": np.full(shape, 4.0),
        "wave_dir_deg": np.full(shape, wind_from_deg),
        "current_speed_kts": np.full(shape, 0.0),
        "current_dir_deg": np.full(shape, 0.0),
    }
    return field


def test_router_reaches_destination_on_beam_reach() -> None:
    polar = Polar.load(DEFAULT_POLAR_PATH)
    charts = NullChartStore()
    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    # Origin → Destination ~20 nm east (about 0.42° at 38° N).
    origin = (38.5, -76.5)
    destination = (38.5, -76.07)
    field = _uniform_field(
        lat_bounds=(38.3, 38.7),
        lon_bounds=(-76.6, -75.9),
        start=depart,
        hours=24,
        wind_kts=12.0,
        wind_from_deg=180.0,   # wind from south → beam reach heading east
    )
    result = plan_candidate(
        origin=origin,
        destination=destination,
        depart_at=depart,
        polar=polar,
        forecast=field,
        charts=charts,
        boat=BoatLimits(),
        step_minutes=30,
        max_steps=48,
        arrival_tolerance_nm=1.0,
    )

    assert len(result.points) >= 3
    assert result.points[0].lat == pytest.approx(origin[0])
    assert result.points[0].lon == pytest.approx(origin[1])
    assert result.points[-1].lat == pytest.approx(destination[0], abs=0.1)
    assert result.points[-1].lon == pytest.approx(destination[1], abs=0.1)
    assert result.reached_at > depart


def test_router_graduated_steps_thread_a_tight_channel() -> None:
    """Near-shore origins need short initial steps to avoid jumping over land.

    The stub chart store below simulates a boat pinned between shores
    by rejecting any segment longer than 3 nm as "crosses land". The
    default 60-minute step at ~5 kts produces ~5 nm segments and would
    always fail; graduated stepping drops to 10-minute (~0.8 nm)
    segments when `distance_to_land_nm` reports the frontier is close
    to shore, which threads the channel cleanly.
    """
    from app.services.geo import distance_nm

    class TightChannelCharts:
        def crosses_land(self, a, b) -> bool:
            return distance_nm(a[0], a[1], b[0], b[1]) > 3.0

        def crosses_obstacle(self, a, b) -> bool:
            return False

        def is_restricted(self, pt) -> bool:
            return False

        def available_depth(self, lat, lon, t):
            return 100.0

        def distance_to_land_nm(self, lat, lon) -> float:
            return 1.0  # always "near shore" → graduated stepping kicks in

        def navaids_in(self, bbox):
            return []

    polar = Polar.load(DEFAULT_POLAR_PATH)
    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    origin = (38.5, -76.5)
    destination = (38.6, -76.5)  # 6 nm north — unreachable in one 60-min jump
    field = _uniform_field(
        lat_bounds=(38.3, 38.7),
        lon_bounds=(-76.6, -76.4),
        start=depart, hours=24,
        wind_kts=15.0, wind_from_deg=270.0,  # beam reach
    )

    # Graduated stepping (default fine_step_minutes=10) succeeds.
    result = plan_candidate(
        origin=origin, destination=destination, depart_at=depart,
        polar=polar, forecast=field, charts=TightChannelCharts(),
        boat=BoatLimits(), arrival_tolerance_nm=0.5,
    )
    assert result.points[-1].lat == pytest.approx(destination[0], abs=0.01)
    assert result.points[-1].lon == pytest.approx(destination[1], abs=0.01)

    # Disabling graduated stepping (fine = coarse = 60min) fails for
    # the same inputs — proves the feature, not a coincidence.
    with pytest.raises(RouterError, match="ROUTE_NO_COVERAGE"):
        plan_candidate(
            origin=origin, destination=destination, depart_at=depart,
            polar=polar, forecast=field, charts=TightChannelCharts(),
            boat=BoatLimits(),
            step_minutes=60, fine_step_minutes=60,
            arrival_tolerance_nm=0.5,
        )


def test_router_raises_on_uncovered_origin() -> None:
    """Origin outside forecast bbox → no frontier → ROUTE_NO_COVERAGE."""
    polar = Polar.load(DEFAULT_POLAR_PATH)
    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    field = _uniform_field(
        lat_bounds=(40.0, 41.0),  # origin 38.5 is outside
        lon_bounds=(-76.6, -75.9),
        start=depart,
        hours=6,
        wind_kts=12.0,
        wind_from_deg=180.0,
    )
    with pytest.raises(RouterError, match="ROUTE_NO_COVERAGE"):
        plan_candidate(
            origin=(38.5, -76.5),
            destination=(38.5, -76.07),
            depart_at=depart,
            polar=polar,
            forecast=field,
            charts=NullChartStore(),
            boat=BoatLimits(),
            step_minutes=30,
            max_steps=6,
        )


def test_router_emits_bv_router_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful plan records steps, wallclock, per-step propagations
    and an `ok` outcome counter increment."""
    from app.services import router as router_mod

    recorded: dict[str, list] = {"steps": [], "prop": [], "wall": [], "out": []}

    class _Rec:
        def __init__(self, bucket: str) -> None:
            self.bucket = bucket

        def record(self, v, labels=None) -> None:
            recorded[self.bucket].append((v, labels))

        def add(self, v, labels=None) -> None:
            recorded[self.bucket].append((v, labels))

    monkeypatch.setattr(router_mod, "_steps", _Rec("steps"))
    monkeypatch.setattr(router_mod, "_propagations_per_step", _Rec("prop"))
    monkeypatch.setattr(router_mod, "_wallclock", _Rec("wall"))
    monkeypatch.setattr(router_mod, "_outcomes", _Rec("out"))

    polar = Polar.load(DEFAULT_POLAR_PATH)
    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    field = _uniform_field(
        lat_bounds=(38.3, 38.7),
        lon_bounds=(-76.6, -75.9),
        start=depart,
        hours=24,
        wind_kts=12.0,
        wind_from_deg=180.0,
    )
    plan_candidate(
        origin=(38.5, -76.5),
        destination=(38.5, -76.07),
        depart_at=depart,
        polar=polar,
        forecast=field,
        charts=NullChartStore(),
        boat=BoatLimits(),
        step_minutes=30,
        max_steps=48,
        arrival_tolerance_nm=1.0,
    )

    assert len(recorded["steps"]) == 1
    assert recorded["steps"][0][0] >= 1
    assert len(recorded["wall"]) == 1
    assert recorded["wall"][0][0] >= 0
    assert len(recorded["prop"]) >= 1
    # Outcome counter fires once with outcome=ok.
    assert len(recorded["out"]) == 1
    assert recorded["out"][0][1]["outcome"] == "ok"


def test_router_outcome_timeout_recorded() -> None:
    """A run that exhausts the step budget emits outcome=timeout."""
    from unittest.mock import patch

    polar = Polar.load(DEFAULT_POLAR_PATH)
    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    field = _uniform_field(
        lat_bounds=(38.3, 38.7),
        lon_bounds=(-76.6, -75.9),
        start=depart,
        hours=24,
        wind_kts=12.0,
        wind_from_deg=180.0,
    )
    with patch("app.services.router._outcomes") as mock_out:
        with pytest.raises(RouterError, match="ROUTE_TIMEOUT"):
            plan_candidate(
                origin=(38.5, -76.5),
                destination=(38.5, -76.07),
                depart_at=depart,
                polar=polar,
                forecast=field,
                charts=NullChartStore(),
                boat=BoatLimits(),
                step_minutes=30,
                max_steps=1,  # force timeout before reaching destination
                arrival_tolerance_nm=0.01,  # tight so 1 step can't land it
            )
        # Timeout increments the outcome counter once with outcome=timeout.
        assert mock_out.add.call_count == 1
        labels = mock_out.add.call_args.args[1]
        assert labels["outcome"] == "timeout"


def test_sector_prune_keeps_one_per_sector() -> None:
    from app.services.router import IsochronePoint

    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    # 40 random-ish points in a small fan ahead of the centroid.
    pts = []
    for i in range(40):
        ang = -40 + i * 2  # -40°..38° from axis
        lat = 38.5 + 0.01 * (i % 5)
        lon = -76.5 + 0.01 * ang / 40 + 0.1
        pts.append(IsochronePoint(lat=lat, lon=lon, t=depart))
    destination = (38.5, -75.0)
    pruned = sector_prune(pts, destination, n_sectors=16, min_frontier_floor=0)
    assert 1 <= len(pruned) <= 16


def test_sector_prune_floors_collapsing_frontier() -> None:
    """When sector bucketing leaves the frontier smaller than the floor,
    supplement with remaining points nearest to destination. Reproduces
    the issue/02 attrition: many valid propagations survive into only a
    handful of buckets, and without a floor the frontier collapses
    within a few more steps.
    """
    from app.services.router import IsochronePoint

    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    destination = (38.20, -76.30)

    pts = [
        IsochronePoint(
            lat=37.90 + 0.003 * (i % 5),
            lon=-76.20 + 0.003 * (i // 5),
            t=depart,
        )
        for i in range(30)
    ]

    bare = sector_prune(pts, destination, n_sectors=4, min_frontier_floor=0)
    floored = sector_prune(pts, destination, n_sectors=4, min_frontier_floor=15)

    assert len(bare) <= 4, f"pre-condition: 4 sectors should cap bare at 4, got {len(bare)}"
    assert len(floored) == 15
    # Every bucketed survivor is still present after the floor kicks in.
    bare_ids = {id(p) for p in bare}
    floored_ids = {id(p) for p in floored}
    assert bare_ids <= floored_ids


def test_sector_prune_retains_obliquely_progressing_points() -> None:
    """A point at ~105° off axis — beyond the 90° bucketing window —
    must still survive when the frontier is thin. The floor fallback
    (nearest-to-destination backfill) is what preserves it.

    Why: widening `half_width` itself to keep such points also lets
    the frontier fan out laterally on every step and eventually
    escape forecast coverage in open-water transits. The floor keeps
    the oblique-progress case without that side-effect.
    """
    from app.services.router import IsochronePoint
    from app.services.geo import advance, bearing_deg

    depart = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    centroid = (38.0, -76.2)
    destination = (38.20, -76.30)
    axis = bearing_deg(centroid[0], centroid[1], destination[0], destination[1])

    oblique_lat, oblique_lon = advance(centroid[0], centroid[1], (axis + 105) % 360, 2.0)
    anchor_lat = 2 * centroid[0] - oblique_lat
    anchor_lon = 2 * centroid[1] - oblique_lon
    pts = [
        IsochronePoint(lat=oblique_lat, lon=oblique_lon, t=depart),
        IsochronePoint(lat=anchor_lat, lon=anchor_lon, t=depart),
    ]

    pruned = sector_prune(pts, destination)  # default floor backfills
    assert any(
        p.lat == oblique_lat and p.lon == oblique_lon for p in pruned
    ), "oblique-progress point dropped — floor backfill regressed"
