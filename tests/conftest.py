import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Set env BEFORE any `app.*` import so `app.db` picks up the test URL.
_BV_TMP = Path(tempfile.mkdtemp(prefix="bv-test-"))
os.environ.setdefault("BV_ENV", "test")
os.environ.setdefault("BV_DATABASE_URL", f"sqlite+aiosqlite:///{_BV_TMP}/test.db")
os.environ.setdefault("BV_LOG_LEVEL", "WARNING")
os.environ.setdefault("BV_OTEL_EXPORTER", "none")
os.environ.setdefault("BV_SUMMARY_MODE", "fallback_only")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db() -> AsyncIterator[None]:
    """Drop + recreate tables before every test for isolation."""
    import app.models  # noqa: F401  register mappers
    from app.db import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture(autouse=True)
async def _reset_http_client() -> AsyncIterator[None]:
    """Force a fresh httpx client per test so pytest-httpx can patch it."""
    from app.clients._http import reset_http_client

    reset_http_client()
    yield
    reset_http_client()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from asgi_lifespan import LifespanManager

    from app.main import app

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
