"""Voyage planner — drives a submission through the job stages.

This module is the bridge between `JobRegistry` (which owns the task
lifecycle) and the real work (charts / forecast / routing / scoring /
GPX emission) that will land across M2-M5. For now, each stage is a
thin stub that writes progress and moves on. The final stage produces
a placeholder GPX blob so the API surface is testable end-to-end; real
candidate routing replaces the stub as router lands later in M2.

`PlannerError` carries the intended job-level error code from plan/10
§errors (CHARTS_NOT_AVAILABLE, FORECAST_UNAVAILABLE, ROUTE_BLOCKED, ...)
so `JobRegistry` can write the right row state on failure.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from app.db import session_scope
from app.logging import get_logger
from app.models.voyage import Voyage
from app.observability import tracer
from app.schemas.request import VoyageRequest
from app.services.jobs import set_stage, write_progress

log = get_logger(__name__)
_tracer = tracer("app.services.planner")


class PlannerError(Exception):
    """Typed job-level failure. Maps to voyages.error_code on terminal
    state; see plan/10 for codes."""

    def __init__(self, code: str, stage: str, detail: str | None = None) -> None:
        super().__init__(f"{code} at {stage}: {detail}")
        self.code = code
        self.stage = stage
        self.detail = detail


@dataclass
class ProgressThrottle:
    """Keep progress writes sane on long runs (plan/16 §Progress shape)."""

    min_interval_s: float
    min_pct_delta: float
    _last_write: float = 0.0
    _last_pct: float = -1.0

    def should_write(self, pct: float) -> bool:
        now = time.monotonic()
        if self._last_pct < 0:
            self._last_write = now
            self._last_pct = pct
            return True
        if (now - self._last_write) >= self.min_interval_s or (
            abs(pct - self._last_pct) >= self.min_pct_delta
        ):
            self._last_write = now
            self._last_pct = pct
            return True
        return False


# --- stage implementations (stubs) ----------------------------------------


async def _stage_charts_fetching(voyage_id: str, req: VoyageRequest) -> None:
    await set_stage(voyage_id, "charts_fetching", pct=0.0, detail="(stub)")
    # Real impl lands with services/charts.py — doc 15-charts-bathymetry.
    with _tracer.start_as_current_span("job.charts_fetching"):
        await asyncio.sleep(0)


async def _stage_charts_preprocessing(voyage_id: str, req: VoyageRequest) -> None:
    await set_stage(voyage_id, "charts_preprocessing", pct=0.0, detail="(stub)")
    with _tracer.start_as_current_span("job.charts_preprocessing"):
        await asyncio.sleep(0)


async def _stage_forecast_prefetching(voyage_id: str, req: VoyageRequest) -> None:
    await set_stage(voyage_id, "forecast_prefetching", pct=0.0, detail="(stub)")
    with _tracer.start_as_current_span("job.forecast_prefetching"):
        await asyncio.sleep(0)


async def _stage_routing(voyage_id: str, req: VoyageRequest) -> None:
    await set_stage(voyage_id, "routing", pct=0.0, detail="(stub)")
    with _tracer.start_as_current_span("job.routing"):
        await asyncio.sleep(0)


async def _stage_scoring(voyage_id: str, req: VoyageRequest) -> None:
    await set_stage(voyage_id, "scoring", pct=0.0, detail="(stub)")
    with _tracer.start_as_current_span("job.scoring"):
        await asyncio.sleep(0)


async def _stage_finalizing(voyage_id: str, req: VoyageRequest) -> None:
    await set_stage(voyage_id, "finalizing", pct=0.0, detail="emitting gpx")
    with _tracer.start_as_current_span("job.finalizing"):
        gpx = _minimal_gpx_blob(req)
        async with session_scope() as session:
            row = await session.get(Voyage, voyage_id)
            if row is None:
                raise LookupError(f"voyage {voyage_id} not found")
            row.gpx_blob = gpx
            row.coverage_json = json.dumps({"forecast": None, "tides": None, "charts": None})


def _minimal_gpx_blob(req: VoyageRequest) -> bytes:
    """Placeholder GPX: just origin → destination rtepts.

    Replaced by services/gpx.py once M2 routing produces real
    candidates.
    """
    from xml.sax.saxutils import escape

    origin_name = escape(req.origin.name or "Origin")
    dest_name = escape(req.destination.name or "Destination")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<gpx version="1.1" creator="better-voyage/0.1"\n'
        f'     xmlns="http://www.topografix.com/GPX/1/1"\n'
        f'     xmlns:bv="https://better-voyage.app/gpx/1">\n'
        f"  <rte>\n"
        f'    <name>{origin_name} → {dest_name}</name>\n'
        f'    <rtept lat="{req.origin.lat}" lon="{req.origin.lon}"><name>{origin_name}</name></rtept>\n'
        f'    <rtept lat="{req.destination.lat}" lon="{req.destination.lon}"><name>{dest_name}</name></rtept>\n'
        f"  </rte>\n"
        f"</gpx>\n"
    ).encode()


# --- top-level entry --------------------------------------------------------


async def run_job(voyage_id: str) -> None:
    """Walk the stages. Raises `PlannerError` on terminal stage failure;
    `JobRegistry._wrap` maps that onto the voyage row."""

    async with session_scope() as session:
        row = await session.get(Voyage, voyage_id)
        if row is None:
            raise LookupError(f"voyage {voyage_id} not found")
        req = VoyageRequest.model_validate_json(row.request_json)

    for stage_fn in (
        _stage_charts_fetching,
        _stage_charts_preprocessing,
        _stage_forecast_prefetching,
        _stage_routing,
        _stage_scoring,
        _stage_finalizing,
    ):
        await stage_fn(voyage_id, req)


__all__ = [
    "PlannerError",
    "ProgressThrottle",
    "run_job",
    "write_progress",
]
