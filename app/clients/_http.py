"""Shared HTTP plumbing for upstream clients.

One `httpx.AsyncClient` per event loop (lazy). Auto-instrumentation
from `HTTPXClientInstrumentor` (wired in `app.observability`) handles
per-request spans, so nothing here needs to touch OTel directly.
"""

from __future__ import annotations

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, constructing it on first use.

    Tests that want to swap in a transport (e.g. `pytest-httpx` or a
    recorded replay) should call `reset_http_client()` and then build
    their own `httpx.AsyncClient(transport=...)` via whichever fixture
    they prefer.
    """
    global _client
    if _client is None:
        s = get_settings()
        _client = httpx.AsyncClient(
            timeout=s.http_timeout_s,
            headers={"User-Agent": s.http_user_agent},
        )
    return _client


def reset_http_client() -> None:
    """Drop the cached client (used in tests to inject a fake transport)."""
    global _client
    _client = None


def retry_policy() -> AsyncRetrying:
    """Standard retry: transport errors + 5xx, exponential backoff."""
    s = get_settings()
    return AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(s.http_retries),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    )


async def get_json(url: str, params: dict[str, str | int | float]) -> dict:
    """GET with retries, raise_for_status, return parsed JSON."""
    client = get_http_client()
    async for attempt in retry_policy():
        with attempt:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    raise RuntimeError("unreachable")  # pragma: no cover
