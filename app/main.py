import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import get_settings
from app.db import engine, init_db
from app.logging import configure_logging, get_logger
from app.observability import setup_observability
from app.routers import boat_profiles, charts, health, pois, voyages
from app.services.boat_profiles import ensure_default_seeded
from app.services.cache_pruner import run_forever as run_cache_pruner
from app.services.charts_fetch import ensure_gebco_available
from app.services.jobs import JobRegistry
from app.services.planner import run_job
from app.ui import router as ui_router

log = get_logger(__name__)


@dataclass
class ChartsReadiness:
    """Coarse-grained state for the chart-data bootstrap.

    `preparing` means the startup auto-download is still running;
    `ready` means the configured GEBCO path exists (either pre-staged
    or freshly downloaded); `failed` surfaces the last error so the UI
    can render a useful message instead of a broken map.

    `phase` / `bytes_done` / `bytes_total` are populated during the
    download so the UI banner can render a progress bar.
    """

    status: Literal["ready", "preparing", "failed"]
    detail: str = ""
    phase: str = ""
    bytes_done: int = 0
    bytes_total: int = 0


def _fmt_gb(n: int) -> str:
    return f"{n / (1024 ** 3):.2f} GB"


async def _prepare_charts(app: FastAPI) -> None:
    settings = get_settings()
    gebco_path = settings.effective_gebco_path()

    def _on_progress(phase: str, done: int, total: int) -> None:
        pct = f"{100 * done / total:.1f}%" if total else "?"
        detail = (
            f"{phase} {_fmt_gb(done)}"
            + (f" / {_fmt_gb(total)} ({pct})" if total else "")
        )
        app.state.charts_ready = ChartsReadiness(
            status="preparing",
            detail=detail,
            phase=phase,
            bytes_done=done,
            bytes_total=total,
        )

    try:
        await ensure_gebco_available(
            gebco_path,
            settings.gebco_download_url,
            on_progress=_on_progress,
        )
    except Exception as exc:
        log.error(
            "charts.bootstrap.failed",
            dest=str(gebco_path),
            error=str(exc),
        )
        app.state.charts_ready = ChartsReadiness(
            status="failed", detail=str(exc)[:300]
        )
        return
    app.state.charts_ready = ChartsReadiness(status="ready")
    log.info("charts.bootstrap.ready", gebco=str(gebco_path))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    setup_observability(app=app, engine=engine)
    await init_db()
    await ensure_default_seeded()

    registry = JobRegistry(runner=run_job)
    await registry.sweep_crashed()
    app.state.registry = registry

    pruner_task = asyncio.create_task(run_cache_pruner(), name="cache-pruner")
    app.state.cache_pruner_task = pruner_task

    settings = get_settings()
    if settings.chart_store_mode != "real":
        # Null mode (tests, offline dev) doesn't need the bathymetry
        # file; mark ready so the UI doesn't render a "preparing" banner.
        app.state.charts_ready = ChartsReadiness(status="ready")
        charts_task: asyncio.Task[None] | None = None
    elif not settings.gebco_auto_download:
        # Operator is managing GEBCO manually. Trust `effective_gebco_path`
        # to exist; if it doesn't, the first voyage will surface
        # `CHARTS_NOT_AVAILABLE` from `locate_gebco_tile`.
        app.state.charts_ready = ChartsReadiness(status="ready")
        charts_task = None
    elif settings.effective_gebco_path().exists():
        app.state.charts_ready = ChartsReadiness(status="ready")
        charts_task = None
    else:
        app.state.charts_ready = ChartsReadiness(
            status="preparing",
            detail=f"downloading GEBCO → {settings.effective_gebco_path()}",
        )
        charts_task = asyncio.create_task(
            _prepare_charts(app), name="charts-bootstrap"
        )
    app.state.charts_bootstrap_task = charts_task

    log.info("better-voyage.startup", version=__version__)
    yield
    pruner_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await pruner_task
    if charts_task is not None:
        charts_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await charts_task
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
app.include_router(charts.router)
app.include_router(ui_router)

_UI_STATIC = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=str(_UI_STATIC)), name="static")
