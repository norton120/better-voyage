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

The API is live at this point, but `POST /voyages` needs chart data —
see [Chart ingest setup](#chart-ingest-setup) below.

## Local development (uv)

```bash
uv sync
uv run uvicorn app.main:app --reload
uv run pytest
uv run ruff check .
uv run mypy app
```

## Chart ingest setup

`better-voyage` refuses to plan without real chart data (plan/15 —
"real charts or no route"). Before the first voyage:

1. Download GEBCO (~8 GB netCDF) from
   <https://www.gebco.net/data_and_products/gridded_bathymetry_data/>.
2. Point the service at it:
   ```bash
   export BV_GEBCO_PATH=/abs/path/to/gebco_2024_sub_ice_topo.nc
   export BV_CHARTS_DIR=./data/charts
   ```
3. Pre-seed a cruising area (optional; first voyage otherwise pays the
   full NOAA ENC + Overpass download on-demand):
   ```bash
   uv run python -m app.charts fetch --region chesapeake
   # or: --bbox lat_min,lon_min,lat_max,lon_max
   ```

For API-exploration only (no real routing), set
`BV_CHART_STORE_MODE=null`. The planner then uses a stub that treats
the whole planet as navigable water — fine for poking at the HTTP
surface, not for actual planning. Full reference in
[`ops/README.md`](./ops/README.md).

## Layout

```
app/        FastAPI application
  charts/   `python -m app.charts` CLI (pre-seed chart cache)
  clients/  External API clients (Open-Meteo, NOAA)
  models/   SQLAlchemy ORM models
  routers/  HTTP endpoints
  schemas/  Pydantic request/response schemas
  services/ Domain logic (routing, charts, scoring, GPX, NL summary)
ops/        Operator tooling
  grafana/  Dashboard JSON for the otel-lgtm Grafana stack
tests/      Pytest suite
plan/       Design & requirements docs
```

## Observability

An optional local Grafana + Tempo + Loki + Prometheus stack ships via
the `observability` compose profile:

```bash
BV_OTEL_EXPORTER=otlp docker compose --profile observability up
# Grafana: http://localhost:3000   (admin / admin)
```

Import [`ops/grafana/better-voyage.json`](./ops/grafana/better-voyage.json)
as a dashboard (Grafana → Dashboards → New → Import). The dashboard
targets the Prometheus datasource that the `grafana/otel-lgtm` image
provisions and panels cover jobs, upstream / cache, router, and
contingencies.
