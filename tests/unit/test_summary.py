"""Summary service tests — digest shape, fallback template, cache flow.

LLM path is unit-tested with monkeypatched `render_llm`; contract
assertions verify shape (length, no markdown) rather than exact text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.schemas.request import Coord, TimeWindow, VoyageRequest
from app.services import summary as summary_svc
from app.services.contingency import BackupDestination, TapOut
from app.services.forecast_field import Env
from app.services.planner import Candidate
from app.services.router import IsochronePoint, RouteResult
from app.services.scorer import Score


def _env(wind_kts: float = 12.0, wind_from: float = 180.0, wave: float = 0.5) -> Env:
    return Env(
        wind_speed_kts=wind_kts,
        wind_dir_deg=wind_from,
        wind_gust_kts=wind_kts * 1.25,
        wave_height_m=wave,
        wave_period_s=4.0,
        wave_dir_deg=wind_from,
        current_speed_kts=0.0,
        current_dir_deg=0.0,
    )


def _candidate(rank: int = 1, night: bool = False) -> Candidate:
    from datetime import timedelta

    depart = datetime(2026, 4, 18, 22 if night else 10, 0, tzinfo=UTC)
    pts = []
    for i in range(6):
        t = depart + timedelta(hours=i)
        pts.append(
            IsochronePoint(
                lat=38.5 + i * 0.01,
                lon=-76.5 + i * 0.05,
                t=t,
                heading_deg=90.0,
                bsp_kts=6.0,
                env=_env(),
            )
        )
    route = RouteResult(
        points=pts,
        reached_at=pts[-1].t,
        steps_used=5,
        objective="fastest",
    )
    return Candidate(
        rank=rank,
        depart_at=depart,
        route=route,
        score=Score(total=82.0, components={}),
    )


def _request() -> VoyageRequest:
    return VoyageRequest(
        origin=Coord(lat=38.5, lon=-76.5, name="Annapolis"),
        destination=Coord(lat=38.5, lon=-76.0, name="Solomons"),
        window=TimeWindow(
            start_at=datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 4, 18, 23, 0, tzinfo=UTC),
            tz="UTC",
        ),
        boat_profile_name="default",
        objective="fastest",
    )


def test_digest_shape() -> None:
    d = summary_svc.digest_candidate(
        candidate=_candidate(), req=_request(), tz_name="UTC"
    )
    assert set(d.keys()) == {"voyage", "candidate", "contingencies"}
    assert d["voyage"]["origin_name"] == "Annapolis"
    assert d["voyage"]["destination_name"] == "Solomons"
    assert d["candidate"]["rank"] == 1
    assert d["candidate"]["duration_h"] == 5.0
    assert d["candidate"]["score"] == 82
    assert d["candidate"]["night_crossing"] is False
    assert "kt" in d["candidate"]["wind_character"]


def test_digest_marks_night_crossing() -> None:
    d = summary_svc.digest_candidate(
        candidate=_candidate(night=True), req=_request(), tz_name="UTC"
    )
    assert d["candidate"]["night_crossing"] is True


def test_digest_contingencies_include_backups_and_tapouts() -> None:
    c = _candidate()
    c.backup_destinations = [
        BackupDestination(name="Solomons", lat=38.58, lon=-76.07, detour_nm=0.5)
    ]
    c.tapouts_by_index = {
        2: [TapOut(name="Deltaville", lat=37.5, lon=-76.3, detour_nm=3.2, type="marina", sym="Marina")]
    }
    d = summary_svc.digest_candidate(candidate=c, req=_request(), tz_name="UTC")
    kinds = {entry["kind"] for entry in d["contingencies"]}
    assert "backup_destination" in kinds
    assert "tap_out_marina" in kinds


def test_fallback_template_is_deterministic() -> None:
    d = summary_svc.digest_candidate(
        candidate=_candidate(), req=_request(), tz_name="UTC"
    )
    text1 = summary_svc.render_fallback(d)
    text2 = summary_svc.render_fallback(d)
    assert text1 == text2
    assert "Depart" in text1
    assert "arrive" in text1
    assert "Score" in text1


@pytest.mark.asyncio
async def test_summarize_falls_back_in_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BV_SUMMARY_MODE", "fallback_only")
    from app.config import get_settings

    get_settings.cache_clear()
    result = await summary_svc.summarize(
        candidate=_candidate(), req=_request(), tz_name="UTC"
    )
    assert result.source == "fallback"
    assert "Score" in result.text


@pytest.mark.asyncio
async def test_summarize_uses_cache_second_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls with identical digest hit the cache on the second."""
    monkeypatch.setenv("BV_SUMMARY_MODE", "llm")
    from app.config import get_settings

    get_settings.cache_clear()

    calls = {"n": 0}

    async def fake_llm(digest: dict[str, Any]) -> tuple[str, int, int]:
        calls["n"] += 1
        return "You'll cruise east on a beam reach - easy day.", 400, 80

    monkeypatch.setattr(summary_svc, "render_llm", fake_llm)

    c = _candidate()
    req = _request()
    r1 = await summary_svc.summarize(candidate=c, req=req, tz_name="UTC")
    r2 = await summary_svc.summarize(candidate=c, req=req, tz_name="UTC")

    assert r1.source == "llm"
    assert r2.source == "cache"
    assert r1.text == r2.text
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_summarize_falls_back_on_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BV_SUMMARY_MODE", "llm")
    from app.config import get_settings

    get_settings.cache_clear()

    async def boom(digest: dict[str, Any]) -> tuple[str, int, int]:
        raise RuntimeError("upstream dead")

    monkeypatch.setattr(summary_svc, "render_llm", boom)

    result = await summary_svc.summarize(
        candidate=_candidate(), req=_request(), tz_name="UTC"
    )
    assert result.source == "fallback"


@pytest.mark.asyncio
async def test_llm_output_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM-produced summaries must pass content contract."""
    monkeypatch.setenv("BV_SUMMARY_MODE", "llm")
    from app.config import get_settings

    get_settings.cache_clear()

    async def fake_llm(digest: dict[str, Any]) -> tuple[str, int, int]:
        return (
            "You'll get a steady 12-15 kt beam reach and arrive before sunset. "
            "Easy day, no surprises.",
            600,
            40,
        )

    monkeypatch.setattr(summary_svc, "render_llm", fake_llm)
    result = await summary_svc.summarize(
        candidate=_candidate(), req=_request(), tz_name="UTC"
    )

    text = result.text
    # Contract: 1-3 sentences, no markdown, no lists.
    sentence_count = text.count(".") + text.count("!") + text.count("?")
    assert 1 <= sentence_count <= 3
    assert "#" not in text
    assert "- " not in text
    assert "*" not in text
