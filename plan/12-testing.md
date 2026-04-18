# 12 — Testing strategy

**Status:** draft

Three tiers. In aggregate they should run in under 30 s locally and hit
no network.

## Tier 1 — unit tests (`tests/unit/`)

Pure-logic tests. No FastAPI, no DB, no network. Fast.

Coverage targets:

- `scorer.py`: each sub-score function + composition. Golden-file checks
  against a table of `(env, expected_score)` rows.
- `router.py`: geometry (bearing, distance, bbox expansion, track
  deviation), POI snapping, land/shoal blocking.
- `contingency.py`: selection algorithm over synthetic POI sets.
- `summary.py`:
  - `digest_candidate()` — golden tests (scored candidate → expected
    compact digest JSON).
  - Fallback templater — exact-string tests.
  - LLM path uses **contract assertions** over recorded responses
    (length 1–3 sentences, mentions local departure, no markdown /
    lists / emoji). Not exact-text matches — LLM output isn't
    bit-deterministic. See doc 08.
- `gpx.py`: emitted XML validates against GPX 1.1 XSD and round-trips.

## Tier 2 — integration tests (`tests/integration/`)

Spin up the FastAPI app in-process with `httpx.AsyncClient`. Use a
SQLite in-memory DB. **Replay** external HTTP via `pytest-httpx` fixtures
saved from real responses.

Coverage:

- `POST /voyages` → `GET /voyages/{id}` → `GET /voyages/{id}/gpx`.
- Idempotency: same request twice returns same `id`.
- Offline: clear the HTTP fixtures, pre-populate the cache DB, re-run —
  must produce the same candidates.
- Error: upstream 500 with no cache → 503 with structured body.

## Tier 3 — replay regression tests

One golden voyage per distinct scenario, committed under
`tests/fixtures/voyages/*.yaml`. Each fixture has:

- input request
- captured HTTP responses (Open-Meteo and NOAA)
- expected candidates (ranked) and their scores (±tolerance)
- expected summary contract (shape/mentions, NOT exact text —
  LLM-generated per doc 08)

When scoring changes, the test suite is expected to fail. Updating the
fixtures is a deliberate, reviewed step (run `uv run python tools/update_fixtures.py <name>`).

## Fixtures & replays

- HTTP fixtures live under `tests/fixtures/http/<source>/<hash>.json`.
- A tiny helper in `tests/conftest.py` auto-registers them with
  `pytest-httpx` based on the test's declared fixture set.

## Properties worth property-testing

Use `hypothesis` for:

- Great-circle distance is symmetric.
- Scorer is monotonic in favorable dimensions (hold everything else
  fixed; raising along-track current shouldn't lower the current
  sub-score).
- Candidate ordering is stable under idempotent re-runs.

## Linting, types, formatting

- `ruff check` in CI + `ruff format --check`.
- `mypy app` with `strict = true`.
- All three must pass on every PR.

## CI

Not MVP, but sketch:

- GitHub Actions: matrix on Python 3.12.
- Steps: `uv sync`, `uv run ruff check .`, `uv run mypy app`, `uv run pytest -q`.
- Cache `~/.cache/uv`.

## Don't mock the DB

Integration tests use a real SQLite (in-memory or tmpdir). Mocking the
ORM layer hides exactly the bugs we want to catch (session lifecycle,
async gotchas, schema drift).

## Open questions

- Do we add a visual "route on a chart" snapshot test via Leaflet
  screenshots? (No — way too heavy for the signal.)
- Do we run a periodic "live" test against real Open-Meteo/NOAA to
  detect schema drift? (Nice to have; keep as a nightly GH Actions job,
  gated from main CI.)
