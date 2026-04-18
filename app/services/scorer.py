"""Candidate scoring — pure, deterministic, numeric.

Per plan/05 §Sub-scores this is a post-hoc 0-100 summary of how
*good* a passage is, distinct from the router's objective function.
Hard limits (max_wind_kts, max_seas_m, min_depth_m) are enforced by
the router; by the time a route reaches scoring it's feasible by
definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from math import cos, radians

from app.services.forecast_field import Env
from app.services.router import IsochronePoint

# Component weights (plan/05 §Default weights)
W = {
    "wind":    0.30,
    "waves":   0.20,
    "swell":   0.10,
    "current": 0.10,
    "tide":    0.10,
    "comfort": 0.10,
    "smg":     0.10,
}


@dataclass
class Score:
    total: float  # 0..100
    components: dict[str, float]


# --- sub-scores -----------------------------------------------------------


def _score_wind(env: Env, course_deg: float) -> float:
    tws = env.wind_speed_kts
    if tws <= 0:
        base = 0.3
    elif tws < 10:
        base = 0.3 + 0.7 * (tws / 10.0)
    elif tws <= 15:
        base = 1.0
    elif tws <= 20:
        base = 1.0 - 0.3 * ((tws - 15) / 5.0)      # 1.0 → 0.7
    elif tws <= 25:
        base = 0.7 - 0.3 * ((tws - 20) / 5.0)      # 0.7 → 0.4
    else:
        base = max(0.0, 0.4 - 0.1 * ((tws - 25) / 5.0))

    rel = _rel_angle(course_deg, env.wind_dir_deg)
    if 70 <= rel <= 110:
        mult = 1.0
    elif 45 <= rel < 70:
        mult = 0.85
    elif 150 <= rel <= 180:
        mult = 0.8
    elif 110 < rel < 150:
        mult = 0.9
    else:  # < 45° close-hauled
        mult = 0.5
    return base * mult


def _score_waves(h_m: float) -> float:
    if h_m < 0.5:
        return 1.0
    if h_m < 1.0:
        return 1.0 - 0.2 * ((h_m - 0.5) / 0.5)
    if h_m < 1.5:
        return 0.9 - 0.15 * ((h_m - 1.0) / 0.5)
    if h_m < 2.0:
        return 0.75 - 0.25 * ((h_m - 1.5) / 0.5)
    if h_m < 2.5:
        return 0.5 - 0.25 * ((h_m - 2.0) / 0.5)
    return max(0.0, 0.25 - 0.25 * ((h_m - 2.5) / 0.5))


def _score_swell(h_m: float, period_s: float) -> float:
    if period_s <= 0:
        return 0.5
    steep = h_m / period_s
    if steep < 0.1:
        return 1.0
    if steep < 0.2:
        return 1.0 - 0.3 * ((steep - 0.1) / 0.1)  # 1.0 -> 0.7
    if steep < 0.3:
        return 0.7 - 0.5 * ((steep - 0.2) / 0.1)  # 0.7 -> 0.2
    return 0.2


def _score_current(env: Env, course_deg: float) -> float:
    # Positive along-track means current helps.
    rel = radians(((env.current_dir_deg - course_deg + 540) % 360) - 180)
    along = env.current_speed_kts * cos(rel)
    # Map [-2, 0, +2] -> [0.2, 0.6, 1.0] linearly.
    clamped = max(-2.0, min(2.0, along))
    return 0.6 + 0.2 * clamped


# --- composition ----------------------------------------------------------


def score_leg(env: Env, course_deg: float) -> dict[str, float]:
    return {
        "wind": _score_wind(env, course_deg),
        "waves": _score_waves(env.wave_height_m),
        "swell": _score_swell(env.wave_height_m, env.wave_period_s),
        "current": _score_current(env, course_deg),
        "tide": 1.0,      # M2: no shallow-waypoint handling yet
        "comfort": 1.0,   # M2: night-sail / duration penalties later
        "smg": 1.0,       # M2: SMG ratio requires best-case-polar plumbing
    }


def score_candidate(route_points: list[IsochronePoint]) -> Score:
    """Weighted mean of leg scores, scaled to 0-100."""
    if len(route_points) < 2:
        return Score(total=0.0, components={k: 0.0 for k in W})

    totals: dict[str, float] = {k: 0.0 for k in W}
    hours_sum = 0.0

    for parent, child in pairwise(route_points):
        if child.env is None or child.heading_deg is None:
            continue
        hours = (child.t - parent.t).total_seconds() / 3600.0
        if hours <= 0:
            continue
        components = score_leg(child.env, child.heading_deg)
        for k, v in components.items():
            totals[k] += v * hours
        hours_sum += hours

    if hours_sum <= 0:
        return Score(total=0.0, components={k: 0.0 for k in W})

    components_norm = {k: totals[k] / hours_sum for k in W}
    total = sum(components_norm[k] * W[k] for k in W) * 100.0
    return Score(total=round(total, 2), components=components_norm)


def _rel_angle(course_deg: float, other_deg: float) -> float:
    rel = (other_deg - course_deg) % 360.0
    if rel > 180.0:
        rel = 360.0 - rel
    return rel
