"""M1 smoke test — fetch Annapolis tide, re-run offline.

Per plan/13 M1: "Smoke: fetch Annapolis tide for tomorrow, re-run
offline." We model "offline" by only registering a single httpx mock
response. pytest-httpx raises on any unmatched request, so if the
second call tried to hit the network the test would fail loudly.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.clients import noaa

ANNAPOLIS_STATION = "8575512"


@pytest.mark.asyncio
async def test_annapolis_tide_cache_survives_offline(httpx_mock) -> None:
    tomorrow = date.today() + timedelta(days=1)

    httpx_mock.add_response(
        url=(
            "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
            f"?product=predictions&application=better-voyage&station={ANNAPOLIS_STATION}"
            f"&begin_date={tomorrow.strftime('%Y%m%d')}"
            f"&end_date={tomorrow.strftime('%Y%m%d')}"
            "&datum=MLLW&units=metric&time_zone=gmt&interval=hilo&format=json"
        ),
        json={
            "predictions": [
                {"t": f"{tomorrow} 01:23", "v": "0.345", "type": "H"},
                {"t": f"{tomorrow} 07:45", "v": "-0.123", "type": "L"},
            ]
        },
    )

    online = await noaa.fetch_tide_predictions(
        station_id=ANNAPOLIS_STATION,
        begin=tomorrow,
        end=tomorrow,
    )
    assert online.stale is False
    assert online.body["predictions"][0]["type"] == "H"

    # No additional mock installed. If the cache didn't take, this call
    # would produce an unmocked request and pytest-httpx would raise.
    offline = await noaa.fetch_tide_predictions(
        station_id=ANNAPOLIS_STATION,
        begin=tomorrow,
        end=tomorrow,
    )
    assert offline.body == online.body
    assert len(httpx_mock.get_requests()) == 1
