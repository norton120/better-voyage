# better-voyage

Context-aware GPX route planner for sailing passages (OpenCPN-compatible).

Given a start point, end point, and a time window, `better-voyage` simulates
candidate departure windows using free public datasets (Open-Meteo Marine and
NOAA Tides & Currents), scores each leg on wind, waves, swell, current, and
tide height, and emits GPX route files with contingency plans (backup
anchorages, "tap-out" marinas, and escape-hatch routes).

See [`plan/`](./plan/README.md) for the detailed design.

## Stack

- Python 3.12 / FastAPI
- SQLAlchemy 2.x (async) + SQLite (aiosqlite) for offline cache
- httpx + tenacity for external APIs
- gpxpy for GPX emission
- pytest / ruff / mypy
- uv for dependency management
- Docker + Compose for local dev and deployment

## Quickstart (Docker)

```bash
docker compose up --build
# FastAPI on http://localhost:8000 — OpenAPI at /docs
curl http://localhost:8000/health
```

## Local development (uv)

```bash
uv sync
uv run uvicorn app.main:app --reload
uv run pytest
uv run ruff check .
uv run mypy app
```

## Layout

```
app/        FastAPI application
  clients/  External API clients (Open-Meteo, NOAA)
  models/   SQLAlchemy ORM models
  routers/  HTTP endpoints
  schemas/  Pydantic request/response schemas
  services/ Domain logic (scoring, planning, GPX)
tests/      Pytest suite
plan/       Design & requirements docs
```
