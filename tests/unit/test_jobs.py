"""Direct tests for JobRegistry — crash recovery + state helpers."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.db import session_scope
from app.models.voyage import Voyage
from app.schemas.request import Coord, TimeWindow, VoyageRequest
from app.services.jobs import (
    JobRegistry,
    find_existing,
    insert_voyage,
    new_voyage_id,
    set_stage,
)


def _make_request() -> VoyageRequest:
    return VoyageRequest(
        origin=Coord(lat=38.9784, lon=-76.4922, name="Annapolis"),
        destination=Coord(lat=36.8467, lon=-76.2929, name="Norfolk"),
        window=TimeWindow(
            start_at=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 4, 27, 0, 0, tzinfo=UTC),
        ),
        boat_profile_name="saltbreaker",
    )


async def _seed_live_voyage(status: str) -> str:
    vid = new_voyage_id()
    async with session_scope() as session:
        session.add(
            Voyage(
                id=vid,
                created_at=datetime.now(UTC),
                status=status,
                progress_json=json.dumps({"stage": status, "pct": 0.5}),
                request_json=_make_request().model_dump_json(),
                inputs_hash="sha256:abcdef",
            )
        )
    return vid


@pytest.mark.asyncio
async def test_sweep_crashed_marks_live_failed() -> None:
    vid = await _seed_live_voyage("routing")

    async def never_called(_: str) -> None:
        raise AssertionError("runner should not run during sweep")

    registry = JobRegistry(runner=never_called)
    await registry.sweep_crashed()

    async with session_scope() as session:
        row = await session.get(Voyage, vid)
    assert row is not None
    assert row.status == "failed"
    assert row.error_code == "WORKER_RESTARTED"
    assert row.error_stage == "routing"


@pytest.mark.asyncio
async def test_sweep_crashed_ignores_terminal_voyages() -> None:
    vid = await _seed_live_voyage("done")

    registry = JobRegistry(runner=lambda _: asyncio.sleep(0))  # type: ignore[arg-type]
    await registry.sweep_crashed()

    async with session_scope() as session:
        row = await session.get(Voyage, vid)
    assert row is not None
    assert row.status == "done"  # unchanged
    assert row.error_code is None


@pytest.mark.asyncio
async def test_registry_runs_runner_to_completion() -> None:
    completed = asyncio.Event()

    async def runner(voyage_id: str) -> None:
        await set_stage(voyage_id, "routing", pct=1.0)
        completed.set()

    registry = JobRegistry(runner=runner)
    vid = await insert_voyage(_make_request())
    task = await registry.submit(vid)
    await task

    assert completed.is_set()
    async with session_scope() as session:
        row = await session.get(Voyage, vid)
    assert row is not None
    assert row.status == "done"
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_insert_voyage_creates_queued_row_with_hash() -> None:
    req = _make_request()
    vid = await insert_voyage(req)
    found = await find_existing()
    assert found is not None
    assert found.id == vid
    assert found.status == "queued"
    assert found.inputs_hash.startswith("sha256:")
