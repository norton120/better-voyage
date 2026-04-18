"""Read-through cache wrapper used by every upstream client.

Pattern (per plan/11-storage-caching.md):

    body = await cache_or_fetch(
        source="open_meteo_marine",
        params={"lat": 38.97, "lon": -76.49, ...},
        store=ForecastCacheStore(),
        ttl_s=settings.forecast_cache_ttl_s,
        fetcher=lambda: open_meteo.fetch(...),
    )

- Cache hit (not expired) → return stored body.
- Miss or expired → call fetcher, store result.
- Upstream error with a stale row present → stale-while-error:
  return the stale body, flag `stale=True`.
- No row + upstream error → re-raise.

Every call emits a `bv.cache.lookups` counter with `source` / `result`
labels and opens a `cache.lookup` span. The metric is the primary
signal for M1 ("every upstream call emits a cache-hit metric" per
plan/13).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from opentelemetry import trace
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.logging import get_logger
from app.models.forecast import ForecastCache, TideCache
from app.observability import meter, tracer

log = get_logger(__name__)
_tracer: trace.Tracer = tracer("app.clients.cache")
_lookups = meter("app.clients.cache").create_counter(
    name="bv.cache.lookups",
    description="Cache lookup outcomes, by source and result",
    unit="1",
)


@dataclass
class CacheRow:
    body_json: str
    fetched_at: datetime
    expires_at: datetime


@dataclass
class CacheResult:
    """Returned to callers so staleness can propagate to coverage."""

    body: Any
    fetched_at: datetime
    stale: bool


class CacheStore(Protocol):
    async def get(self, session: AsyncSession, key: str) -> CacheRow | None: ...
    async def put(
        self,
        session: AsyncSession,
        key: str,
        *,
        params_json: str,
        body_json: str,
        fetched_at: datetime,
        expires_at: datetime,
        extras: dict[str, Any],
    ) -> None: ...


class ForecastCacheStore:
    async def get(self, session: AsyncSession, key: str) -> CacheRow | None:
        row = await session.get(ForecastCache, key)
        if row is None:
            return None
        return CacheRow(
            body_json=row.body_json,
            fetched_at=_as_aware(row.fetched_at),
            expires_at=_as_aware(row.expires_at),
        )

    async def put(
        self,
        session: AsyncSession,
        key: str,
        *,
        params_json: str,
        body_json: str,
        fetched_at: datetime,
        expires_at: datetime,
        extras: dict[str, Any],
    ) -> None:
        stmt = sqlite_insert(ForecastCache).values(
            key=key,
            params_json=params_json,
            body_json=body_json,
            fetched_at=fetched_at,
            expires_at=expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ForecastCache.key],
            set_={
                "params_json": stmt.excluded.params_json,
                "body_json": stmt.excluded.body_json,
                "fetched_at": stmt.excluded.fetched_at,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        await session.execute(stmt)


class TideCacheStore:
    async def get(self, session: AsyncSession, key: str) -> CacheRow | None:
        row = await session.get(TideCache, key)
        if row is None:
            return None
        return CacheRow(
            body_json=row.body_json,
            fetched_at=_as_aware(row.fetched_at),
            expires_at=_as_aware(row.expires_at),
        )

    async def put(
        self,
        session: AsyncSession,
        key: str,
        *,
        params_json: str,
        body_json: str,
        fetched_at: datetime,
        expires_at: datetime,
        extras: dict[str, Any],
    ) -> None:
        station_id = extras.get("station_id")
        if not station_id:
            raise ValueError("TideCacheStore requires extras['station_id']")
        stmt = sqlite_insert(TideCache).values(
            key=key,
            station_id=station_id,
            body_json=body_json,
            fetched_at=fetched_at,
            expires_at=expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[TideCache.key],
            set_={
                "station_id": stmt.excluded.station_id,
                "body_json": stmt.excluded.body_json,
                "fetched_at": stmt.excluded.fetched_at,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        await session.execute(stmt)


def params_hash(params: dict[str, Any]) -> str:
    """Canonical hash of a params dict. Stable across dict ordering."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def as_aware_utc(dt: datetime) -> datetime:
    """SQLite may hand back naive datetimes; treat them as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# Back-compat alias for internal callers.
_as_aware = as_aware_utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def _record(source: str, result: str) -> None:
    _lookups.add(1, {"source": source, "result": result})


async def cache_or_fetch(
    *,
    source: str,
    params: dict[str, Any],
    store: CacheStore,
    ttl_s: int,
    fetcher: Callable[[], Awaitable[Any]],
    extras: dict[str, Any] | None = None,
    key_override: str | None = None,
) -> CacheResult:
    """Cache-or-fetch with TTL and stale-while-error.

    `fetcher` is called only on miss / expiry. Its return value is
    JSON-serialized via `json.dumps(..., default=str)` and stored; the
    parsed form is returned to the caller.

    `extras` is passed through to `store.put` for fields that vary per
    table (e.g. `station_id` for tide rows).

    `key_override` lets a caller supply a human-readable key instead
    of the default `<source>:<params_hash>` (e.g. stations keyed by
    station id).
    """
    key = key_override or f"{source}:{params_hash(params)}"
    extras = extras or {}

    with _tracer.start_as_current_span(
        "cache.lookup",
        attributes={"cache.source": source, "cache.key": key},
    ) as span:
        async with session_scope() as session:
            existing = await store.get(session, key)
            now = utc_now()

            if existing and existing.expires_at > now:
                span.set_attribute("cache.result", "hit")
                _record(source, "hit")
                return CacheResult(
                    body=json.loads(existing.body_json),
                    fetched_at=existing.fetched_at,
                    stale=False,
                )

            outcome = "miss" if existing is None else "refresh"
            try:
                body = await fetcher()
            except Exception as exc:
                if existing is not None:
                    age = (now - existing.fetched_at).total_seconds()
                    span.set_attribute("cache.result", "stale")
                    span.set_attribute("cache.stale_age_s", age)
                    _record(source, "stale")
                    log.warning(
                        "cache.stale_while_error",
                        source=source,
                        key=key,
                        age_s=age,
                        error=str(exc),
                    )
                    return CacheResult(
                        body=json.loads(existing.body_json),
                        fetched_at=existing.fetched_at,
                        stale=True,
                    )
                span.set_attribute("cache.result", "error")
                _record(source, "error")
                raise

            body_json = json.dumps(body, default=str)
            expires_at = now + timedelta(seconds=ttl_s)
            await store.put(
                session,
                key,
                params_json=json.dumps(params, sort_keys=True, default=str),
                body_json=body_json,
                fetched_at=now,
                expires_at=expires_at,
                extras=extras,
            )

            span.set_attribute("cache.result", outcome)
            _record(source, outcome)
            return CacheResult(body=body, fetched_at=now, stale=False)
