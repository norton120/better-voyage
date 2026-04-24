"""HTMX UI smoke test.

Exercises the three UI endpoints:

- `GET /` serves the HTML shell (Leaflet + form + empty status slot).
- `POST /ui/voyages` accepts a form-encoded submission, spawns a
  voyage, and returns the status partial.
- `GET /ui/voyages/{id}/status` is the polling target.

We don't drive the voyage to `done` here — the end-to-end plumbing
already ships under `test_end_to_end.py`; this test only asserts that
the UI-specific surface is wired and returns HTML.
"""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_index_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert '<div id="map"></div>' in html
    assert 'hx-post="/ui/voyages"' in html
    assert 'src="/static/ui.js"' in html
    assert 'leaflet@1.9.4' in html


@pytest.mark.asyncio
async def test_static_assets_served(client: AsyncClient) -> None:
    js = await client.get("/static/ui.js")
    assert js.status_code == 200
    assert "L.map" in js.text
    css = await client.get("/static/ui.css")
    assert css.status_code == 200
    assert "#map" in css.text


@pytest.mark.asyncio
async def test_ui_submit_returns_status_partial(client: AsyncClient) -> None:
    # The job task runs async in the background; this test returns
    # before forecast prefetch fires, so we don't need upstream mocks.
    form = {
        "origin_lat": "38.9",
        "origin_lon": "-76.5",
        "destination_lat": "38.5",
        "destination_lon": "-76.3",
        "origin_name": "",
        "destination_name": "",
        "start_at": "2026-05-01T00:00",
        "end_at": "2026-05-02T00:00",
        "tz": "UTC",
        "boat_profile_name": "default",
        "objective": "fastest",
        "max_candidates": "2",
        "earliest_local": "",
        "latest_local": "",
    }
    # Non-HTMX client: submission 303s to the detail page. HTMX
    # clients get an `HX-Redirect` header with a 200 body (tested
    # separately below). Either way the destination is /v/{id}.
    resp = await client.post("/ui/voyages", data=form, follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/v/vy_"), location
    vid = location[len("/v/"):]

    detail = await client.get(location)
    assert detail.status_code == 200
    html = detail.text
    # Detail page embeds the status partial; its hx-get points back at
    # /ui/voyages/{id}/status and includes an adaptive poll interval
    # (never the old fixed 2s).
    assert f'hx-get="/ui/voyages/{vid}/status"' in html
    assert re.search(r'hx-trigger="every \d+s"', html)
    assert "class=\"badge badge-" in html

    poll = await client.get(f"/ui/voyages/{vid}/status")
    assert poll.status_code == 200
    assert poll.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_ui_submit_htmx_uses_hx_redirect(client: AsyncClient) -> None:
    """HTMX clients get an HX-Redirect header instead of a 303. The
    client-side library follows it as a hard navigation."""
    form = {
        "origin_lat": "38.9", "origin_lon": "-76.5",
        "destination_lat": "38.5", "destination_lon": "-76.3",
        "origin_name": "", "destination_name": "",
        "start_at": "2026-05-01T00:00",
        "end_at": "2026-05-02T00:00",
        "tz": "UTC",
        "boat_profile_name": "default",
        "objective": "fastest",
        "max_candidates": "2",
        "earliest_local": "", "latest_local": "",
    }
    resp = await client.post(
        "/ui/voyages", data=form, headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"].startswith("/v/vy_")


@pytest.mark.asyncio
async def test_ui_submit_rejects_bad_window(client: AsyncClient) -> None:
    form = {
        "origin_lat": "38.9",
        "origin_lon": "-76.5",
        "destination_lat": "38.5",
        "destination_lon": "-76.3",
        "origin_name": "",
        "destination_name": "",
        "start_at": "2026-05-02T00:00",
        "end_at": "2026-05-01T00:00",  # end before start
        "tz": "UTC",
        "boat_profile_name": "default",
        "objective": "fastest",
        "max_candidates": "2",
        "earliest_local": "",
        "latest_local": "",
    }
    resp = await client.post("/ui/voyages", data=form)
    assert resp.status_code == 400
    assert "INVALID_WINDOW" in resp.text


@pytest.mark.asyncio
async def test_ui_submit_unknown_boat_profile(client: AsyncClient) -> None:
    form = {
        "origin_lat": "38.9",
        "origin_lon": "-76.5",
        "destination_lat": "38.5",
        "destination_lon": "-76.3",
        "origin_name": "",
        "destination_name": "",
        "start_at": "2026-05-01T00:00",
        "end_at": "2026-05-02T00:00",
        "tz": "UTC",
        "boat_profile_name": "no-such-boat",
        "objective": "fastest",
        "max_candidates": "2",
        "earliest_local": "",
        "latest_local": "",
    }
    resp = await client.post("/ui/voyages", data=form)
    assert resp.status_code == 404
    assert "BOAT_PROFILE_NOT_FOUND" in resp.text
