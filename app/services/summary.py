"""Natural-language candidate summaries (plan/08-nl-summary.md).

Three-layer design:

- `digest_candidate(candidate, req, tz)` — pure function that turns a
  scored route into the compact JSON the model sees. Golden-tested.
- `render_fallback(digest)` — deterministic template. Used when the
  Anthropic SDK fails, is absent, or `BV_SUMMARY_MODE=fallback_only`.
- `render_llm(digest)` — one Anthropic Messages call (Haiku 4.5).
  System prompt marked with `cache_control` so repeated candidate
  calls across one voyage read the cached prefix.

`summarize(candidate, req, tz)` is the orchestrator: cache → LLM →
fallback. Results land in SQLite (`summary_cache`) keyed by
`sha256(digest + prompt_version + model)`; TTL matches the forecast
cache since the digest includes forecast-derived characterizations.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import cos, radians
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.clients.cache import as_aware_utc, utc_now
from app.config import get_settings
from app.db import session_scope
from app.logging import get_logger
from app.models.forecast import SummaryCache
from app.observability import meter, tracer

log = get_logger(__name__)
_tracer = tracer("app.services.summary")
_m = meter("app.services.summary")
_requests = _m.create_counter("bv.summary.requests", unit="1")
_failures = _m.create_counter("bv.summary.failures", unit="1")
_tokens = _m.create_counter("bv.summary.tokens", unit="1")

_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "data" / "prompts" / "summary_system.md"
_system_prompt_cache: str | None = None


@dataclass(frozen=True)
class Summary:
    text: str
    source: str  # "llm" | "cache" | "fallback"


def load_system_prompt() -> str:
    """Read the system prompt once; cached on the module."""
    global _system_prompt_cache
    if _system_prompt_cache is None:
        _system_prompt_cache = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    return _system_prompt_cache


# --- digest ---------------------------------------------------------------


def _local_label(t: datetime, tz: ZoneInfo) -> str:
    """Format as 'Mon 09:00 EDT'."""
    return t.astimezone(tz).strftime("%a %H:%M %Z").strip()


def _wind_character(env_samples: list) -> str:
    if not env_samples:
        return "no forecast data"
    kts = [e.wind_speed_kts for e in env_samples if e is not None]
    if not kts:
        return "no forecast data"
    lo, hi = min(kts), max(kts)
    return f"{round(lo)}-{round(hi)} kt" if hi - lo >= 2 else f"steady {round(lo)} kt"


def _seas_character(env_samples: list) -> str:
    heights = [e.wave_height_m for e in env_samples if e is not None]
    if not heights:
        return "no forecast data"
    lo, hi = min(heights), max(heights)
    if hi < 0.5:
        return "calm under 0.5 m"
    if hi - lo < 0.3:
        return f"steady {hi:.1f} m"
    return f"{lo:.1f}-{hi:.1f} m"


def _current_character(env_samples: list, courses: list[float | None]) -> str:
    if not env_samples or not courses:
        return "no forecast data"
    favorable = 0
    against = 0
    for e, c in zip(env_samples, courses, strict=False):
        if e is None or c is None:
            continue
        rel = radians(((e.current_dir_deg - c + 540) % 360) - 180)
        along = e.current_speed_kts * cos(rel)
        if along > 0.1:
            favorable += 1
        elif along < -0.1:
            against += 1
    total = favorable + against
    if total == 0:
        return "neutral"
    if favorable > against * 2:
        return "mostly favorable"
    if against > favorable * 2:
        return "mostly against"
    return f"{favorable} favorable / {against} against hours"


def _tack_count(route_points: list) -> int:
    tacks = 0
    prev_heading: float | None = None
    for p in route_points:
        if p.heading_deg is None:
            continue
        if prev_heading is not None:
            diff = abs(p.heading_deg - prev_heading)
            diff = min(diff, 360 - diff)
            if diff > 60:
                tacks += 1
        prev_heading = p.heading_deg
    return tacks


def _night_crossing(route_points: list, tz: ZoneInfo) -> bool:
    for p in route_points:
        h = p.t.astimezone(tz).hour
        if h >= 22 or h < 6:
            return True
    return False


def digest_candidate(
    *,
    candidate,
    req,
    tz_name: str,
) -> dict[str, Any]:
    """Build the JSON payload for the LLM. Pure, deterministic."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    route = candidate.route
    duration_h = round(
        (route.reached_at - candidate.depart_at).total_seconds() / 3600.0, 2
    )

    env_samples = [p.env for p in route.points if p.env is not None]
    courses = [p.heading_deg for p in route.points if p.heading_deg is not None]

    contingencies: list[dict[str, Any]] = []
    for b in candidate.backup_destinations[:1]:
        contingencies.append(
            {"kind": "backup_destination", "target": b.name, "detour_nm": b.detour_nm}
        )
    for _idx, tapouts in candidate.tapouts_by_index.items():
        if not tapouts:
            continue
        contingencies.append(
            {"kind": "tap_out_marina", "target": tapouts[0].name, "detour_nm": tapouts[0].detour_nm}
        )
        if len(contingencies) >= 3:
            break

    return {
        "voyage": {
            "origin_name": req.origin.name or "Origin",
            "destination_name": req.destination.name or "Destination",
            "objective": req.objective,
        },
        "candidate": {
            "rank": candidate.rank,
            "depart_at_local": _local_label(candidate.depart_at, tz),
            "arrive_at_local": _local_label(route.reached_at, tz),
            "duration_h": duration_h,
            "score": round(candidate.score.total),
            "night_crossing": _night_crossing(route.points, tz),
            "wind_character": _wind_character(env_samples),
            "seas_character": _seas_character(env_samples),
            "current_character": _current_character(env_samples, courses),
            "tack_count": _tack_count(route.points),
        },
        "contingencies": contingencies,
    }


# --- fallback -------------------------------------------------------------


def render_fallback(digest: dict[str, Any]) -> str:
    c = digest["candidate"]
    return (
        f"Depart {c['depart_at_local']}, arrive {c['arrive_at_local']} "
        f"({c['duration_h']:.0f} h). Score {c['score']:.0f}/100."
    )


# --- cache ----------------------------------------------------------------


def _cache_key(digest: dict[str, Any], prompt_version: str, model: str) -> str:
    payload = json.dumps(digest, sort_keys=True, separators=(",", ":"))
    combined = f"{payload}|{prompt_version}|{model}"
    return "summary:" + hashlib.sha256(combined.encode("utf-8")).hexdigest()[:32]


async def _cache_get(key: str) -> str | None:
    async with session_scope() as session:
        row = await session.get(SummaryCache, key)
    if row is None:
        return None
    if as_aware_utc(row.expires_at) <= utc_now():
        return None
    return row.summary_md


async def _cache_put(
    key: str, model: str, summary_md: str, tokens_in: int, tokens_out: int
) -> None:
    settings = get_settings()
    now = utc_now()
    async with session_scope() as session:
        existing = await session.get(SummaryCache, key)
        if existing is not None:
            await session.delete(existing)
        session.add(
            SummaryCache(
                key=key,
                model=model,
                summary_md=summary_md,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                fetched_at=now,
                expires_at=now + timedelta(seconds=settings.forecast_cache_ttl_s),
            )
        )


# --- LLM ------------------------------------------------------------------


async def render_llm(digest: dict[str, Any]) -> tuple[str, int, int]:
    """Call Claude Haiku. Returns (text, tokens_in, tokens_out).

    Raises on any failure — orchestrator catches and falls back.
    """
    import anthropic

    settings = get_settings()
    system_prompt = load_system_prompt()
    user_content = json.dumps(digest, sort_keys=True, indent=2)

    client = anthropic.AsyncAnthropic(timeout=settings.summary_timeout_s)
    with _tracer.start_as_current_span(
        "summary.render.llm",
        attributes={"bv.summary.model": settings.summary_model},
    ):
        resp = await client.messages.create(
            model=settings.summary_model,
            max_tokens=settings.summary_max_tokens,
            temperature=settings.summary_temperature,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )

    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += block.text
    text = text.strip()
    tokens_in = int(getattr(resp.usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(resp.usage, "output_tokens", 0) or 0)
    _tokens.add(tokens_in, {"direction": "in"})
    _tokens.add(tokens_out, {"direction": "out"})
    return text, tokens_in, tokens_out


# --- orchestrator ---------------------------------------------------------


async def summarize(
    *,
    candidate,
    req,
    tz_name: str,
) -> Summary:
    settings = get_settings()
    digest = digest_candidate(candidate=candidate, req=req, tz_name=tz_name)
    key = _cache_key(digest, settings.summary_prompt_version, settings.summary_model)

    # 1. Cache lookup.
    cached = await _cache_get(key)
    if cached is not None:
        _requests.add(1, {"source": "cache"})
        return Summary(text=cached, source="cache")

    # 2. Fallback-only mode.
    if settings.summary_mode == "fallback_only":
        text = render_fallback(digest)
        _requests.add(1, {"source": "fallback"})
        return Summary(text=text, source="fallback")

    # 3. LLM call.
    started = time.monotonic()
    try:
        text, tin, tout = await render_llm(digest)
        if not text:
            raise RuntimeError("llm returned empty text")
        await _cache_put(key, settings.summary_model, text, tin, tout)
        _requests.add(1, {"source": "llm"})
        log.info(
            "summary.done",
            source="llm",
            tokens_in=tin,
            tokens_out=tout,
            duration_s=round(time.monotonic() - started, 3),
        )
        return Summary(text=text, source="llm")
    except Exception as exc:
        _failures.add(1, {"reason": type(exc).__name__})
        _requests.add(1, {"source": "fallback"})
        log.warning("summary.llm_fallback", error=str(exc)[:200])
        return Summary(text=render_fallback(digest), source="fallback")
