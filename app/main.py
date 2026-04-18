from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.db import engine, init_db
from app.logging import configure_logging, get_logger
from app.observability import setup_observability
from app.routers import boat_profiles, health, pois, voyages
from app.services.boat_profiles import ensure_default_seeded
from app.services.cache_pruner import run_forever as run_cache_pruner
from app.services.jobs import JobRegistry
from app.services.planner import run_job
from app.ui import router as ui_router

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    setup_observability(app=app, engine=engine)
    await init_db()
    await ensure_default_seeded()

    registry = JobRegistry(runner=run_job)
    await registry.sweep_crashed()
    app.state.registry = registry

    import asyncio
    import contextlib

    pruner_task = asyncio.create_task(run_cache_pruner(), name="cache-pruner")
    app.state.cache_pruner_task = pruner_task

    log.info("better-voyage.startup", version=__version__)
    yield
    pruner_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await pruner_task
    await registry.shutdown()
    log.info("better-voyage.shutdown")


app = FastAPI(
    title="better-voyage",
    version=__version__,
    description="Context-aware GPX route planner for sailing passages.",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(voyages.router)
app.include_router(boat_profiles.router)
app.include_router(pois.router)
app.include_router(ui_router)

_UI_STATIC = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=str(_UI_STATIC)), name="static")
