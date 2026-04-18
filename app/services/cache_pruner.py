"""Hourly cache pruning task (plan/11 §Cache pruning).

Deletes forecast / tide / station / summary rows past expires_at. Runs
under an asyncio task registered on app.state.cache_pruner_task by the
FastAPI lifespan; teardown cancels it.

Voyages are NOT pruned — retention is enforced synchronously on every
POST /voyages (doc 11 §Retention — single-voyage model).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete

from app.clients.cache import utc_now
from app.db import session_scope
from app.logging import get_logger
from app.models.forecast import ForecastCache, StationsCache, SummaryCache, TideCache
from app.observability import meter

log = get_logger(__name__)
_pruned = meter("app.services.cache_pruner").create_counter(
    "bv.cache.pruned", description="Rows deleted by the cache pruner", unit="1"
)

DEFAULT_INTERVAL_S = 3600.0  # 1 hour


async def prune_once() -> dict[str, int]:
    """Run one pruning pass. Returns per-source delete counts."""
    now = utc_now()
    counts: dict[str, int] = {}
    async with session_scope() as session:
        for source, model in (
            ("forecast", ForecastCache),
            ("tide", TideCache),
            ("stations", StationsCache),
            ("summary", SummaryCache),
        ):
            result = await session.execute(
                delete(model).where(model.expires_at < now)
            )
            counts[source] = result.rowcount or 0
            if counts[source]:
                _pruned.add(counts[source], {"source": source})
    if any(counts.values()):
        log.info("cache.pruned", **counts)
    return counts


async def run_forever(interval_s: float = DEFAULT_INTERVAL_S) -> None:
    """Prune on boot, then every `interval_s`. Cancelled at shutdown."""
    try:
        await prune_once()
        while True:
            await asyncio.sleep(interval_s)
            await prune_once()
    except asyncio.CancelledError:
        log.debug("cache.pruner.stopped")
        raise
