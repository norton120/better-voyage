from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Make sure the parent directory of a file-backed SQLite DB exists."""
    if ":memory:" in database_url:
        return
    parsed = urlparse(database_url)
    # aiosqlite URL: sqlite+aiosqlite:///./data/foo.db → path="/./data/foo.db"
    path = parsed.path.lstrip("/")
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent_dir(_settings.database_url)

engine = create_async_engine(_settings.database_url, echo=False, future=True)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables declared on `Base.metadata`.

    Alembic migrations take over once the voyages state machine lands
    (M2); until then this keeps the cache tables in sync with the
    models for local dev + tests.
    """
    import app.models  # noqa: F401  register mappers

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
