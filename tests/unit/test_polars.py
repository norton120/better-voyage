"""Polar loader + bilinear interpolation tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.services.polars import DEFAULT_POLAR_PATH, Polar


def _toy() -> Polar:
    # 3 TWAs x 2 TWSs, hand-picked so interpolation is easy to verify.
    return Polar(
        twa_deg=np.array([0.0, 90.0, 180.0]),
        tws_kts=np.array([5.0, 10.0]),
        bsp_kts=np.array([[0.0, 0.0], [4.0, 8.0], [2.0, 5.0]]),
    )


def test_exact_grid_points() -> None:
    p = _toy()
    assert p.bsp(0, 5) == 0.0
    assert p.bsp(90, 5) == 4.0
    assert p.bsp(90, 10) == 8.0
    assert p.bsp(180, 10) == 5.0


def test_interpolates_in_tws() -> None:
    p = _toy()
    # halfway between 5 and 10 kts at TWA=90: (4+8)/2 = 6
    assert p.bsp(90, 7.5) == pytest.approx(6.0)


def test_interpolates_in_twa() -> None:
    p = _toy()
    # halfway between 90 and 180 at TWS=5: (4+2)/2 = 3
    assert p.bsp(135, 5) == pytest.approx(3.0)


def test_interpolates_bilinearly() -> None:
    p = _toy()
    # (135, 7.5) — center of the 90-180 x 5-10 cell
    # corners: 4, 8, 2, 5  → mean = 4.75
    assert p.bsp(135, 7.5) == pytest.approx(4.75)


def test_symmetric_in_twa_sign() -> None:
    p = _toy()
    assert p.bsp(-90, 7.5) == pytest.approx(p.bsp(90, 7.5))
    assert p.bsp(-135, 5) == pytest.approx(p.bsp(135, 5))


def test_tws_below_min_clamps() -> None:
    p = _toy()
    # No wind floor in the toy polar — min col is 5 kts. 1 kt clamps to 5 kt.
    assert p.bsp(90, 1) == 4.0


def test_tws_above_max_clamps() -> None:
    p = _toy()
    # 15 kt clamps to 10 kt; no bonus for extra wind.
    assert p.bsp(90, 15) == 8.0


def test_twa_outside_polar_returns_zero() -> None:
    # A narrow polar (40-180) returns 0 outside its TWA range.
    narrow = Polar(
        twa_deg=np.array([40.0, 180.0]),
        tws_kts=np.array([5.0, 10.0]),
        bsp_kts=np.array([[3.0, 6.0], [2.0, 4.0]]),
    )
    assert narrow.bsp(0, 5) == 0.0
    assert narrow.bsp(30, 5) == 0.0


def test_loads_ships_default_polar() -> None:
    p = Polar.load(DEFAULT_POLAR_PATH)
    # Sanity — beam reach at mid wind should be a realistic number for a
    # 40-ft cruiser. If someone edits the file into nonsense, this fires.
    assert 5.0 < p.bsp(90, 12) < 9.0
    assert p.bsp(0, 10) == 0.0  # in irons


def test_bsp_rejects_malformed_csv(tmp_path: Path) -> None:
    f = tmp_path / "bad.pol"
    f.write_text("TWA\\TWS;4;8\n90;5;6\n45;3;4\n")  # TWA not increasing
    with pytest.raises(ValueError, match="TWA"):
        Polar.load(f)
