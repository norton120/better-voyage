"""NOAA CO-OPS Tides & Currents client.

Two upstreams:

- **Tide predictions** — `api/prod/datagetter?product=predictions`,
  cached in `tide_cache` with 24 h TTL (doc 03 §NOAA, doc 11).
- **Station metadata** — `mdapi/prod/webapi`, cached in
  `stations_cache` with 30 d TTL.

Currents from NOAA are deliberately NOT used (doc 03); ocean currents
come from Open-Meteo's continuous grid instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.clients._http import get_json
from app.clients.cache import (
    CacheResult,
    TideCacheStore,
    as_aware_utc,
    cache_or_fetch,
    utc_now,
)
from app.config import get_settings
from app.db import session_scope
from app.logging import get_logger
from app.models.forecast import StationsCache
from app.observability import meter, tracer

log = get_logger(__name__)
_tracer = tracer("app.clients.noaa")
_lookups = meter("app.clients.noaa").create_counter(
    name="bv.cache.lookups",
    description="Cache lookup outcomes, by source and result",
    unit="1",
)

_TIDE_SOURCE = "noaa_tides"
_STATIONS_SOURCE = "noaa_stations"

Datum = str  # "MLLW" | "MSL" | "MLW" | ...
Interval = str  # "hilo" | "h" | "6" | ...


@dataclass
class Station:
    id: str
    kind: str  # "tide" | "current"
    lat: float
    lon: float
    name: str


def _to_compact_date(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return d.strftime("%Y%m%d")
    return d.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Tide predictions — TTL-cached request/response
# ---------------------------------------------------------------------------


async def fetch_tide_predictions(
    station_id: str,
    begin: date | datetime,
    end: date | datetime,
    *,
    datum: Datum = "MLLW",
    interval: Interval = "hilo",
    units: str = "metric",
) -> CacheResult:
    """Fetch tide predictions for `station_id` between `begin` and `end`.

    Default `interval="hilo"` returns only high/low events; use `"h"`
    for hourly samples.
    """
    settings = get_settings()
    params: dict[str, str | int | float] = {
        "product": "predictions",
        "application": "better-voyage",
        "station": station_id,
        "begin_date": _to_compact_date(begin),
        "end_date": _to_compact_date(end),
        "datum": datum,
        "units": units,
        "time_zone": "gmt",
        "interval": interval,
        "format": "json",
    }
    key = (
        f"{_TIDE_SOURCE}:{station_id}:"
        f"{params['begin_date']}-{params['end_date']}:{interval}"
    )
    return await cache_or_fetch(
        source=_TIDE_SOURCE,
        params=params,
        store=TideCacheStore(),
        ttl_s=settings.tide_cache_ttl_s,
        fetcher=lambda: get_json(settings.noaa_tides_base_url, params),
        extras={"station_id": station_id},
        key_override=key,
    )


# ---------------------------------------------------------------------------
# Station metadata — stored one row per station in stations_cache
# ---------------------------------------------------------------------------


async def list_tide_stations(*, refresh: bool = False) -> list[Station]:
    """Return every NOAA tide-prediction station.

    Results are mirrored into `stations_cache` for cheap spatial
    lookups later. If the cache has any fresh rows of kind="tide" and
    `refresh=False`, we serve from cache without hitting the network.
    """
    settings = get_settings()
    kind = "tide"

    if not refresh:
        async with session_scope() as session:
            result = await session.execute(
                select(StationsCache).where(
                    StationsCache.kind == kind,
                    StationsCache.expires_at > utc_now(),
                )
            )
            rows = result.scalars().all()
        if rows:
            _lookups.add(1, {"source": _STATIONS_SOURCE, "result": "hit"})
            return [
                Station(id=r.id, kind=r.kind, lat=r.lat, lon=r.lon, name=r.name)
                for r in rows
            ]

    with _tracer.start_as_current_span("noaa.list_stations"):
        url = f"{settings.noaa_metadata_base_url}/stations.json"
        body = await get_json(url, {"type": "tidepredictions"})

    stations: list[Station] = [
        Station(
            id=str(entry["id"]),
            kind=kind,
            lat=float(entry["lat"]),
            lon=float(entry.get("lng", entry.get("lon"))),
            name=str(entry.get("name", "")),
        )
        for entry in body.get("stations", [])
    ]

    expires_at = utc_now() + timedelta(seconds=settings.stations_cache_ttl_s)
    async with session_scope() as session:
        for s in stations:
            stmt = sqlite_insert(StationsCache).values(
                id=s.id,
                kind=s.kind,
                lat=s.lat,
                lon=s.lon,
                name=s.name,
                payload=json.dumps({"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon}),
                expires_at=expires_at,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[StationsCache.id],
                set_={
                    "kind": stmt.excluded.kind,
                    "lat": stmt.excluded.lat,
                    "lon": stmt.excluded.lon,
                    "name": stmt.excluded.name,
                    "payload": stmt.excluded.payload,
                    "expires_at": stmt.excluded.expires_at,
                },
            )
            await session.execute(stmt)

    _lookups.add(1, {"source": _STATIONS_SOURCE, "result": "miss"})
    log.info("noaa.stations_refreshed", count=len(stations))
    return stations


async def get_station(station_id: str) -> Station | None:
    """Lookup a single station by id. Cache-first, no network on hit."""
    async with session_scope() as session:
        row = await session.get(StationsCache, station_id)
        if row is not None and as_aware_utc(row.expires_at) > utc_now():
            _lookups.add(1, {"source": _STATIONS_SOURCE, "result": "hit"})
            return Station(
                id=row.id, kind=row.kind, lat=row.lat, lon=row.lon, name=row.name
            )

    settings = get_settings()
    url = f"{settings.noaa_metadata_base_url}/stations/{station_id}.json"
    body = await get_json(url, {})
    entries = body.get("stations") or []
    if not entries:
        return None
    entry = entries[0]
    station = Station(
        id=str(entry["id"]),
        kind="tide",
        lat=float(entry["lat"]),
        lon=float(entry.get("lng", entry.get("lon"))),
        name=str(entry.get("name", "")),
    )

    expires_at = utc_now() + timedelta(seconds=settings.stations_cache_ttl_s)
    async with session_scope() as session:
        stmt = sqlite_insert(StationsCache).values(
            id=station.id,
            kind=station.kind,
            lat=station.lat,
            lon=station.lon,
            name=station.name,
            payload=json.dumps(entry, default=str),
            expires_at=expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[StationsCache.id],
            set_={
                "kind": stmt.excluded.kind,
                "lat": stmt.excluded.lat,
                "lon": stmt.excluded.lon,
                "name": stmt.excluded.name,
                "payload": stmt.excluded.payload,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        await session.execute(stmt)

    _lookups.add(1, {"source": _STATIONS_SOURCE, "result": "miss"})
    return station
