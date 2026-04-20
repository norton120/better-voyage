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
# FastAPI on http://localhost:8000
#   UI:     http://localhost:8000/
#   OpenAPI: http://localhost:8000/docs
curl http://localhost:8000/health
```

The API is live at this point, but `POST /voyages` needs chart data —
see [Chart ingest setup](#chart-ingest-setup) below.

## UI

A minimal HTMX + Leaflet page at `/` — click the map to pick origin
then destination, fill the window / boat / objective form, submit,
watch progress, and download any candidate's GPX. The UI is a thin
face over the JSON API; use Swagger (`/docs`) for anything the form
doesn't expose.

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
"real charts or no route"). The first time the app starts, it
auto-downloads the global GEBCO bathymetry grid (~8 GB) to
`$BV_CHARTS_DIR/gebco/GEBCO_2024_sub_ice_topo.nc` and the UI shows a
"preparing charts" page until the download finishes. No env vars are
required for a basic dev setup.

Optional overrides:

1. Pre-stage GEBCO yourself (e.g. download once on a fast connection,
   ship the file around) and point the service at it:
   ```bash
   export BV_GEBCO_PATH=/abs/path/to/gebco_2024_sub_ice_topo.nc
   ```
   If `BV_GEBCO_PATH` points at an existing file the auto-download is
   skipped.
2. Disable auto-download entirely (e.g. air-gapped ops):
   ```bash
   export BV_GEBCO_AUTO_DOWNLOAD=false
   ```
   Voyages fail with `CHARTS_NOT_AVAILABLE` until GEBCO is staged.
3. Pre-seed a cruising area (first voyage otherwise pays the full NOAA
   ENC + Overpass download on-demand):
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
  ui/       HTMX + Leaflet skipper UI (templates + static)
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
