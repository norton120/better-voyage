"""Cache pruner deletes expired rows across all cache tables."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.clients.cache import utc_now
from app.db import session_scope
from app.models.forecast import ForecastCache, SummaryCache, TideCache
from app.services.cache_pruner import prune_once


@pytest.mark.asyncio
async def test_prune_once_deletes_expired_and_keeps_fresh() -> None:
    now = utc_now()
    async with session_scope() as session:
        session.add_all(
            [
                ForecastCache(
                    key="fc-expired",
                    params_json="{}",
                    body_json="{}",
                    fetched_at=now - timedelta(hours=5),
                    expires_at=now - timedelta(hours=1),
                ),
                ForecastCache(
                    key="fc-fresh",
                    params_json="{}",
                    body_json="{}",
                    fetched_at=now,
                    expires_at=now + timedelta(hours=1),
                ),
                TideCache(
                    key="tc-expired",
                    station_id="8575512",
                    body_json="{}",
                    fetched_at=now - timedelta(days=2),
                    expires_at=now - timedelta(days=1),
                ),
                SummaryCache(
                    key="sc-expired",
                    model="claude-haiku-4-5",
                    summary_md="old",
                    tokens_in=100,
                    tokens_out=20,
                    fetched_at=now - timedelta(hours=5),
                    expires_at=now - timedelta(hours=1),
                ),
                SummaryCache(
                    key="sc-fresh",
                    model="claude-haiku-4-5",
                    summary_md="new",
                    tokens_in=100,
                    tokens_out=20,
                    fetched_at=now,
                    expires_at=now + timedelta(hours=1),
                ),
            ]
        )

    counts = await prune_once()
    assert counts["forecast"] == 1
    assert counts["tide"] == 1
    assert counts["summary"] == 1

    async with session_scope() as session:
        remaining_fc = (
            await session.execute(select(ForecastCache.key))
        ).scalars().all()
        remaining_tc = (
            await session.execute(select(TideCache.key))
        ).scalars().all()
        remaining_sc = (
            await session.execute(select(SummaryCache.key))
        ).scalars().all()

    assert remaining_fc == ["fc-fresh"]
    assert remaining_tc == []
    assert remaining_sc == ["sc-fresh"]


@pytest.mark.asyncio
async def test_prune_noop_on_empty_tables() -> None:
    counts = await prune_once()
    assert all(v == 0 for v in counts.values())
