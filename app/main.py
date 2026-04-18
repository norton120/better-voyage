from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.db import engine, init_db
from app.logging import configure_logging, get_logger
from app.observability import setup_observability
from app.routers import health, voyages
from app.services.jobs import JobRegistry
from app.services.planner import run_job

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    setup_observability(app=app, engine=engine)
    await init_db()

    registry = JobRegistry(runner=run_job)
    await registry.sweep_crashed()
    app.state.registry = registry

    log.info("better-voyage.startup", version=__version__)
    yield
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
