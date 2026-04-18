"""Scorer unit tests."""

from __future__ import annotations

import pytest

from app.services.forecast_field import Env
from app.services.scorer import (
    W,
    _score_current,
    _score_swell,
    _score_waves,
    _score_wind,
    score_leg,
)


def _env(
    wind_kts: float = 12.0,
    wind_from: float = 180.0,
    wave_h: float = 0.5,
    wave_p: float = 4.0,
    current_kts: float = 0.0,
    current_deg: float = 0.0,
) -> Env:
    return Env(
        wind_speed_kts=wind_kts,
        wind_dir_deg=wind_from,
        wind_gust_kts=wind_kts * 1.25,
        wave_height_m=wave_h,
        wave_period_s=wave_p,
        wave_dir_deg=wind_from,
        current_speed_kts=current_kts,
        current_dir_deg=current_deg,
    )


def test_wind_peak_at_beam_reach_12kt() -> None:
    # Course east, wind from south → beam reach, 12 kt → peak.
    assert _score_wind(_env(wind_kts=12, wind_from=180), course_deg=90) == pytest.approx(1.0)


def test_wind_close_hauled_penalized() -> None:
    # Course north, wind from N-NW → close-hauled (rel ~30°).
    close_hauled = _score_wind(_env(wind_kts=12, wind_from=30), course_deg=0)
    beam = _score_wind(_env(wind_kts=12, wind_from=90), course_deg=0)
    assert close_hauled < beam


def test_wind_high_tws_drops() -> None:
    low = _score_wind(_env(wind_kts=12, wind_from=180), 90)
    high = _score_wind(_env(wind_kts=25, wind_from=180), 90)
    assert high < low


def test_waves_calm_is_max() -> None:
    assert _score_waves(0.3) == 1.0
    assert _score_waves(2.5) < 0.5


def test_swell_steeper_is_worse() -> None:
    # 1 m at 10 s = steep 0.1 — long rollers (good)
    calm = _score_swell(1.0, 10.0)
    # 2 m at 5 s = steep 0.4 — confused (bad)
    confused = _score_swell(2.0, 5.0)
    assert calm > confused


def test_current_along_helps() -> None:
    # Course east, current flowing east at 2 kt.
    helping = _score_current(_env(current_kts=2.0, current_deg=90), course_deg=90)
    # Current against (flowing west).
    against = _score_current(_env(current_kts=2.0, current_deg=270), course_deg=90)
    assert helping > against
    assert helping == pytest.approx(1.0)
    assert against == pytest.approx(0.2)


def test_score_leg_returns_all_components() -> None:
    components = score_leg(_env(), course_deg=90)
    assert set(components.keys()) == set(W.keys())
    for v in components.values():
        assert 0.0 <= v <= 1.0
