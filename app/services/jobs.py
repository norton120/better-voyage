"""Async job registry for voyage planning.

The FastAPI lifespan owns a single `JobRegistry`. Submitting a voyage
spawns an `asyncio.Task` wrapped around the planner; the semaphore
throttles concurrent jobs to `BV_MAX_CONCURRENT_JOBS`. Progress and
terminal state live in the `voyages` row — the task is authoritative
about its own lifecycle but the DB is authoritative about outcomes.

Crash recovery: `sweep_crashed()` runs at startup and marks any row
in a live stage as `failed` with `WORKER_RESTARTED` per plan/16.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select, update
from ulid import ULID

from app.clients.cache import utc_now
from app.config import get_settings
from app.db import session_scope
from app.logging import get_logger
from app.models.voyage import Voyage
from app.observability import meter, tracer
from app.schemas.request import VoyageRequest, compute_inputs_hash

log = get_logger(__name__)
_tracer = tracer("app.services.jobs")
_m = meter("app.services.jobs")

_submitted = _m.create_counter("bv.jobs.submitted", unit="1")
_completed = _m.create_counter("bv.jobs.completed", unit="1")
_stage_transitions = _m.create_counter("bv.jobs.stage_transitions", unit="1")
_duration = _m.create_histogram(
    "bv.jobs.duration_seconds",
    description="End-to-end duration of a voyage job, from wrap-start to terminal status",
    unit="s",
)
_stage_duration = _m.create_histogram(
    "bv.jobs.stage_duration_seconds",
    description="Time spent in each stage, recorded on transition out",
    unit="s",
)
_failures = _m.create_counter(
    "bv.jobs.failures",
    description="Job-terminal failures, by error_code",
    unit="1",
)

LIVE_STAGES: frozenset[str] = frozenset(
    {
        "charts_fetching",
        "charts_preprocessing",
        "forecast_prefetching",
        "routing",
        "scoring",
        "finalizing",
        "cancelling",
    }
)
TERMINAL_STATES: frozenset[str] = frozenset({"done", "failed", "cancelled"})


JobRunner = Callable[[str], Awaitable[None]]


def new_voyage_id() -> str:
    return f"vy_{ULID()}"


class JobRegistry:
    def __init__(self, runner: JobRunner) -> None:
        self._runner = runner
        settings = get_settings()
        self._sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------

    async def sweep_crashed(self) -> None:
        """Mark any live-stage voyage as failed with WORKER_RESTARTED.

        Called from the FastAPI lifespan before any new submissions.
        No-op when the table is empty or all rows are terminal.
        """
        now = utc_now()
        async with session_scope() as session:
            result = await session.execute(
                select(Voyage).where(Voyage.status.in_(LIVE_STAGES))
            )
            rows = result.scalars().all()
            for row in rows:
                prior_stage = row.status
                row.status = "failed"
                row.error_code = "WORKER_RESTARTED"
                row.error_stage = prior_stage
                row.error_detail = "Process restarted while job was live."
                row.completed_at = now
                log.warning(
                    "jobs.crash_recovery",
                    voyage_id=row.id,
                    prior_stage=prior_stage,
                )

    async def shutdown(self) -> None:
        """Cancel all running jobs, wait for them to unwind."""
        async with self._task_lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    # --- submission --------------------------------------------------

    async def submit(self, voyage_id: str) -> asyncio.Task[None]:
        """Spawn the job task.

        The caller is expected to have already inserted the `queued`
        row. The task blocks on the semaphore if all slots are taken.
        """
        _submitted.add(1)
        task = asyncio.create_task(self._wrap(voyage_id), name=f"voyage:{voyage_id}")
        async with self._task_lock:
            self._tasks[voyage_id] = task
        return task

    async def _wrap(self, voyage_id: str) -> None:
        started_at: datetime | None = None
        status_label = "done"
        wall_start = time.monotonic()
        try:
            async with self._sem:
                started_at = utc_now()
                wall_start = time.monotonic()
                await self._mark_started(voyage_id, started_at)
                with _tracer.start_as_current_span(
                    "voyage.job", attributes={"voyage.id": voyage_id}
                ):
                    await self._runner(voyage_id)
            await self._mark_completed(voyage_id, "done")
            _completed.add(1, {"status": "done"})
        except asyncio.CancelledError:
            status_label = "cancelled"
            await self._mark_completed(voyage_id, "cancelled")
            _completed.add(1, {"status": "cancelled"})
            raise
        except Exception as exc:
            status_label = "failed"
            await self._mark_failed(voyage_id, exc)
            _completed.add(1, {"status": "failed"})
        finally:
            if started_at is not None:
                _duration.record(
                    time.monotonic() - wall_start,
                    {"status": status_label},
                )
            async with self._task_lock:
                self._tasks.pop(voyage_id, None)

    async def _mark_started(self, voyage_id: str, when: datetime) -> None:
        async with session_scope() as session:
            await session.execute(
                update(Voyage)
                .where(Voyage.id == voyage_id)
                .values(started_at=when)
            )

    async def _mark_completed(self, voyage_id: str, status: str) -> None:
        async with session_scope() as session:
            await session.execute(
                update(Voyage)
                .where(Voyage.id == voyage_id)
                .values(status=status, completed_at=utc_now())
            )

    async def _mark_failed(self, voyage_id: str, exc: BaseException) -> None:
        # `PlannerError` below carries the intended error code; otherwise
        # we fall back to a generic INTERNAL_ERROR.
        from app.services.planner import PlannerError

        code = "INTERNAL_ERROR"
        stage: str | None = None
        detail = str(exc)[:500]
        if isinstance(exc, PlannerError):
            code = exc.code
            stage = exc.stage
            detail = exc.detail or detail

        _failures.add(1, {"error_code": code})
        log.error(
            "jobs.failed",
            voyage_id=voyage_id,
            error_code=code,
            error_stage=stage,
            detail=detail,
        )
        async with session_scope() as session:
            await session.execute(
                update(Voyage)
                .where(Voyage.id == voyage_id)
                .values(
                    status="failed",
                    completed_at=utc_now(),
                    error_code=code,
                    error_detail=detail,
                    error_stage=stage,
                )
            )

    # --- cancellation ------------------------------------------------

    async def cancel(self, voyage_id: str) -> bool:
        """Mark row cancelling, signal the task, wait for it to unwind.

        Returns True if a live task was cancelled, False if the voyage
        was already terminal (caller maps that to a 200 no-op per
        plan/10 §cancel).
        """
        async with session_scope() as session:
            row = await session.get(Voyage, voyage_id)
            if row is None or row.status in TERMINAL_STATES:
                return False
            row.status = "cancelling"

        async with self._task_lock:
            task = self._tasks.get(voyage_id)
        if task is None:
            # Registry doesn't know about this one — treat as crashed.
            async with session_scope() as session:
                await session.execute(
                    update(Voyage)
                    .where(Voyage.id == voyage_id)
                    .values(status="cancelled", completed_at=utc_now())
                )
            return True

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True


# ---------------------------------------------------------------------------
# Stage-transition helper shared with planner.py
# ---------------------------------------------------------------------------


# Per-voyage stage entry timestamps, keyed by (voyage_id, stage). Read on
# transition-out to record `bv.jobs.stage_duration_seconds`; entries are
# popped as they're consumed so the dict self-cleans during a normal run
# and the stale rows cost ~hundreds of bytes if a worker dies mid-stage.
_stage_entry_at: dict[tuple[str, str], float] = {}


async def set_stage(voyage_id: str, stage: str, *, pct: float = 0.0, detail: str | None = None) -> None:
    """Flip `status` + write an initial progress blob for the stage."""
    async with session_scope() as session:
        row = await session.get(Voyage, voyage_id)
        if row is None:
            raise LookupError(f"voyage {voyage_id} not found")
        prev = row.status
        row.status = stage
        row.progress_json = json.dumps({"stage": stage, "pct": pct, "detail": detail})
    now = time.monotonic()
    entry = _stage_entry_at.pop((voyage_id, prev), None)
    if entry is not None and prev in LIVE_STAGES:
        _stage_duration.record(now - entry, {"stage": prev})
    _stage_entry_at[(voyage_id, stage)] = now
    _stage_transitions.add(1, {"from": prev, "to": stage})
    log.info("jobs.stage", voyage_id=voyage_id, stage=stage, pct=pct, detail=detail)


async def write_progress(
    voyage_id: str, stage: str, pct: float, detail: str | None = None, eta_s: float | None = None
) -> None:
    """Update the progress blob within the current stage. Callers are
    responsible for rate limiting (`ProgressThrottle` in planner.py)."""
    async with session_scope() as session:
        row = await session.get(Voyage, voyage_id)
        if row is None:
            return
        row.progress_json = json.dumps(
            {"stage": stage, "pct": pct, "detail": detail, "eta_s": eta_s}
        )


# ---------------------------------------------------------------------------
# Idempotency helpers (used by the HTTP handler)
# ---------------------------------------------------------------------------


async def find_existing() -> Voyage | None:
    """The table holds at most one row; return it if present."""
    async with session_scope() as session:
        result = await session.execute(select(Voyage).limit(1))
        return result.scalar_one_or_none()


async def insert_voyage(req: VoyageRequest) -> str:
    """Insert a fresh queued row, return its id."""
    vid = new_voyage_id()
    now = utc_now()
    async with session_scope() as session:
        session.add(
            Voyage(
                id=vid,
                created_at=now,
                status="queued",
                progress_json=json.dumps({"stage": "queued", "pct": 0}),
                request_json=req.model_dump_json(),
                inputs_hash=compute_inputs_hash(req),
            )
        )
    return vid


async def delete_voyage(voyage_id: str) -> None:
    async with session_scope() as session:
        row = await session.get(Voyage, voyage_id)
        if row is not None:
            await session.delete(row)
