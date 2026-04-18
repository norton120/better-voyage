"""Contingency selection tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import pois as pois_svc
from app.services.contingency import (
    DECISION_POINT_INTERVAL_H,
    decision_points,
    find_backup_destinations,
    find_tapouts,
)
from app.services.router import IsochronePoint


@pytest.fixture(autouse=True)
def _inject_pois(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a tight POI set so tap-outs and backups have hits at test scale."""
    test_pois = [
        pois_svc.POI(lat=38.500, lon=-76.495, name="Anchorage-A", sym="Anchor", type="anchorage"),
        pois_svc.POI(lat=38.502, lon=-76.300, name="Marina-B", sym="Marina", type="marina"),
        pois_svc.POI(lat=38.510, lon=-76.100, name="Refuge-C", sym="Marina", type="harbor_of_refuge"),
        pois_svc.POI(lat=38.600, lon=-76.200, name="Hazard-X", sym="Shoal", type="hazard"),
    ]
    monkeypatch.setattr(pois_svc, "_cache", test_pois)


def _route(n: int, step_h: int = 2) -> list[IsochronePoint]:
    start = datetime(2026, 4, 18, 0, 0, tzinfo=UTC)
    return [
        IsochronePoint(
            lat=38.5,
            lon=-76.5 + i * 0.05,
            t=start + timedelta(hours=i * step_h),
            heading_deg=90.0,
            bsp_kts=6.0,
        )
        for i in range(n)
    ]


def test_decision_points_pick_every_4h() -> None:
    # 8-hour passage at 2h per step = 5 points (0, 2, 4, 6, 8).
    pts = _route(5, step_h=2)
    picks = decision_points(pts, every_hours=DECISION_POINT_INTERVAL_H)
    # We skip origin (idx 0) and destination (idx -1); of {2h, 4h, 6h},
    # 4h is the first mark >= 4h.
    assert len(picks) >= 1
    elapsed = [(p.t - pts[0].t).total_seconds() / 3600 for p in picks]
    # All picks are past the 4h threshold and before the end.
    for e in elapsed:
        assert 4.0 <= e <= 6.0


def test_decision_points_empty_for_short_route() -> None:
    # A 2-point route has no intermediate points.
    pts = _route(2)
    assert decision_points(pts) == []


def test_find_backup_destinations_filters_by_radius_and_drops_exact() -> None:
    # Destination exactly on Anchorage-A (0 nm) — dropped; remaining POIs
    # within 5 nm of Anchorage-A: Marina-B is ~9.1 nm east, Refuge-C is
    # ~19 nm east. None within 5 nm → empty list.
    empty = find_backup_destinations(38.500, -76.495)
    assert empty == []

    # Move destination east so Refuge-C is within 5nm.
    near = find_backup_destinations(38.510, -76.060)
    names = [b.name for b in near]
    assert "Refuge-C" in names


def test_find_tapouts_filters_hazards_and_ranks_by_distance() -> None:
    # Decision point roughly between Anchorage-A and Marina-B.
    rtept = IsochronePoint(
        lat=38.500, lon=-76.350, t=datetime(2026, 4, 18, 4, 0, tzinfo=UTC),
    )
    out = find_tapouts(rtept)
    names = [t.name for t in out]
    # Marina-B is closest; Anchorage-A second; Hazard-X (wrong type) omitted.
    assert "Marina-B" in names
    assert "Anchorage-A" in names
    assert "Hazard-X" not in names
    # Sorted by detour_nm ascending.
    assert out == sorted(out, key=lambda t: t.detour_nm)
