"""Unit tests for the TTL cache wrapper.

No HTTP — fetcher is a pure-python async function we control. Verifies
hit / miss / refresh / stale-while-error semantics and that fetcher is
only called when it should be.
"""

from __future__ import annotations

import pytest

from app.clients.cache import (
    CacheResult,
    ForecastCacheStore,
    cache_or_fetch,
    params_hash,
)


@pytest.mark.asyncio
async def test_miss_then_hit_does_not_call_fetcher_twice() -> None:
    calls = 0

    async def fetcher() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    params = {"a": 1, "b": "x"}

    first: CacheResult = await cache_or_fetch(
        source="test",
        params=params,
        store=ForecastCacheStore(),
        ttl_s=60,
        fetcher=fetcher,
    )
    assert first.body == {"value": 1}
    assert first.stale is False
    assert calls == 1

    second: CacheResult = await cache_or_fetch(
        source="test",
        params=params,
        store=ForecastCacheStore(),
        ttl_s=60,
        fetcher=fetcher,
    )
    assert second.body == {"value": 1}
    assert second.stale is False
    assert calls == 1  # served from cache


@pytest.mark.asyncio
async def test_expired_refetches() -> None:
    calls = 0

    async def fetcher() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    params = {"station": "A"}
    await cache_or_fetch(
        source="test",
        params=params,
        store=ForecastCacheStore(),
        ttl_s=0,  # immediate expiry
        fetcher=fetcher,
    )
    await cache_or_fetch(
        source="test",
        params=params,
        store=ForecastCacheStore(),
        ttl_s=0,
        fetcher=fetcher,
    )
    assert calls == 2


@pytest.mark.asyncio
async def test_stale_while_error_returns_prior_body() -> None:
    calls = 0

    async def ok() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    async def boom() -> dict[str, int]:
        raise RuntimeError("upstream dead")

    params = {"key": "stale"}
    first = await cache_or_fetch(
        source="test",
        params=params,
        store=ForecastCacheStore(),
        ttl_s=0,  # already stale next time
        fetcher=ok,
    )
    assert first.body == {"value": 1}

    second = await cache_or_fetch(
        source="test",
        params=params,
        store=ForecastCacheStore(),
        ttl_s=0,
        fetcher=boom,
    )
    assert second.body == {"value": 1}
    assert second.stale is True


@pytest.mark.asyncio
async def test_no_cache_plus_error_raises() -> None:
    async def boom() -> dict[str, int]:
        raise RuntimeError("upstream dead")

    with pytest.raises(RuntimeError, match="upstream dead"):
        await cache_or_fetch(
            source="test",
            params={"cold": "start"},
            store=ForecastCacheStore(),
            ttl_s=60,
            fetcher=boom,
        )


def test_params_hash_is_order_independent() -> None:
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})
    assert params_hash({"a": 1}) != params_hash({"a": 2})
