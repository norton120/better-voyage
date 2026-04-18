"""Chart fetchers (downloaders) — NOAA ENC, OpenSeaMap / Overpass, GEBCO.

This is a **leaf** module: it handles the "download bytes + persist to
disk" side of chart ingest. Nothing here parses S-57 or OSM semantics —
that's the preprocessor's job (plan/15 §Preprocessing). Nothing here
decides whether to fetch; that's `ChartStore.ensure_coverage`.

Three entry points:

- `fetch_enc_cells(bbox, out_dir)` — parse NOAA's ENC catalog, filter
  by bbox, download + unzip each intersecting cell.
- `fetch_osm_extract(bbox, out_dir)` — Overpass POST, save the returned
  OSM XML for the coastline + seamark features we actually use.
- `locate_gebco_tile(gebco_path)` — validate an operator-supplied path.
  GEBCO is ~8 GB; we never download it.

HTTP + retry + cache-on-disk style mirrors `app.clients.open_meteo` /
`app.clients._http`: one shared `httpx.AsyncClient`, tenacity retries
on transport errors and 5xx, OTel spans + bv.charts.* metrics on every
fetch. Callers can inject a custom `client` for tests.

Errors map to the voyage error codes in plan/15 §Failure modes:

- `ChartsFetchError`  → `CHARTS_FETCH_FAILED`
- `ChartsCoverageError` → `CHARTS_NOT_AVAILABLE`
"""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.logging import get_logger
from app.observability import meter, tracer

Bbox = tuple[float, float, float, float]  # lat_min, lon_min, lat_max, lon_max


log = get_logger(__name__)
_tracer = tracer("app.services.charts_fetch")
_m = meter("app.services.charts_fetch")
_fetch_bytes = _m.create_counter(
    "bv.charts.fetch_bytes",
    description="Bytes downloaded by chart fetchers, by source",
    unit="By",
)
_fetch_seconds = _m.create_histogram(
    "bv.charts.fetch_seconds",
    description="Wall-clock seconds per chart fetch, by source",
    unit="s",
)


class ChartsFetchError(Exception):
    """Upstream network or parse failure. Maps to CHARTS_FETCH_FAILED."""


class ChartsCoverageError(Exception):
    """Bbox has gaps ENC ∪ OSM can't cover, or GEBCO tile unavailable.

    Maps to CHARTS_NOT_AVAILABLE.
    """  # noqa: RUF002


@dataclass(frozen=True)
class EncCellFetchResult:
    cell_id: str
    s57_path: Path
    fetched_at: datetime
    bytes_downloaded: int


@dataclass(frozen=True)
class OsmExtractFetchResult:
    extract_id: str
    pbf_path: Path  # actually a `.osm` file — the preprocessor accepts both
    fetched_at: datetime
    bytes_downloaded: int


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _bbox_hash(bbox: Bbox) -> str:
    lat_min, lon_min, lat_max, lon_max = bbox
    raw = f"{lat_min},{lon_min},{lat_max},{lon_max}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _retry_policy() -> AsyncRetrying:
    """Match `app.clients._http.retry_policy`."""
    s = get_settings()
    return AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(s.http_retries),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    )


def _owned_client() -> httpx.AsyncClient:
    """Build a caller-owned client matching the shared one's defaults.

    We don't reuse `app.clients._http.get_http_client()` here because
    the process-wide client outlives this function; closing it would
    break other callers. When the caller passes their own `client`,
    we don't close it.
    """
    s = get_settings()
    return httpx.AsyncClient(
        timeout=s.http_timeout_s,
        headers={"User-Agent": s.http_user_agent},
    )


def _bboxes_intersect(a: Bbox, b: Bbox) -> bool:
    a_lat_min, a_lon_min, a_lat_max, a_lon_max = a
    b_lat_min, b_lon_min, b_lat_max, b_lon_max = b
    return not (
        a_lat_max < b_lat_min
        or a_lat_min > b_lat_max
        or a_lon_max < b_lon_min
        or a_lon_min > b_lon_max
    )


def _fetched_at_file(dir_: Path) -> Path:
    return dir_ / "fetched_at.txt"


def _read_fetched_at(dir_: Path) -> datetime | None:
    f = _fetched_at_file(dir_)
    if not f.exists():
        return None
    try:
        return datetime.fromisoformat(f.read_text().strip())
    except ValueError:
        return None


def _write_fetched_at(dir_: Path, when: datetime) -> None:
    _fetched_at_file(dir_).write_text(when.isoformat())


def _is_fresh(fetched_at: datetime | None, max_age_days: int) -> bool:
    if fetched_at is None:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return _utc_now() - fetched_at < timedelta(days=max_age_days)


# --------------------------------------------------------------------------- #
# NOAA ENC catalog parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _CatalogCell:
    name: str
    bbox: Bbox  # lat_min, lon_min, lat_max, lon_max (panel extent)


def _parse_catalog(xml_bytes: bytes) -> list[_CatalogCell]:
    """Parse NOAA's ENCProdCat.xml into a list of (name, bbox) rows.

    The catalog shape per plan/15 §Data sources / NOAA ENC:

        <catalog>
          <cell>
            <name>US4MD01M</name>
            <cov>
              <panel>
                <vertex>lat,lon</vertex>
                ...
              </panel>
            </cov>
          </cell>
          ...
        </catalog>

    A cell's bbox is the axis-aligned envelope of all panel vertices.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ChartsFetchError(f"NOAA ENC catalog is not valid XML: {exc}") from exc

    cells: list[_CatalogCell] = []
    for cell_el in root.iter("cell"):
        name_el = cell_el.find("name")
        if name_el is None or not (name_el.text or "").strip():
            continue
        name = (name_el.text or "").strip()
        lats: list[float] = []
        lons: list[float] = []
        for vertex in cell_el.iter("vertex"):
            txt = (vertex.text or "").strip()
            if not txt or "," not in txt:
                continue
            lat_s, lon_s = txt.split(",", 1)
            try:
                lats.append(float(lat_s))
                lons.append(float(lon_s))
            except ValueError:
                continue
        if not lats or not lons:
            continue
        cells.append(
            _CatalogCell(
                name=name,
                bbox=(min(lats), min(lons), max(lats), max(lons)),
            )
        )
    return cells


async def _fetch_catalog_xml(
    client: httpx.AsyncClient, cache_path: Path, *, max_age_days: int
) -> bytes:
    """Fetch (or reuse) the NOAA ENC catalog XML.

    Cache-on-disk with `max_age_days` TTL. If the upstream fails AND we
    have a cached copy, we reuse it (stale-while-error). If both fail,
    raise `ChartsFetchError`.
    """
    settings = get_settings()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    have_cache = cache_path.exists()
    cache_mtime = (
        datetime.fromtimestamp(cache_path.stat().st_mtime, tz=UTC)
        if have_cache
        else None
    )
    if have_cache and _is_fresh(cache_mtime, max_age_days):
        return cache_path.read_bytes()

    url = settings.noaa_enc_catalog_url
    try:
        async for attempt in _retry_policy():
            with attempt:
                resp = await client.get(url)
                resp.raise_for_status()
                body = resp.content
                cache_path.write_bytes(body)
                return body
    except Exception as exc:
        if have_cache:
            log.warning(
                "charts.catalog.stale_while_error",
                url=url,
                error=str(exc),
            )
            return cache_path.read_bytes()
        raise ChartsFetchError(f"failed to fetch NOAA ENC catalog: {exc}") from exc
    raise ChartsFetchError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------- #
# ENC: fetch_enc_cells
# --------------------------------------------------------------------------- #


def _enc_zip_url(cell_name: str) -> str:
    """NOAA publishes each cell at /ENCs/{cell}.zip alongside the catalog."""
    settings = get_settings()
    base = settings.noaa_enc_catalog_url.rsplit("/", 1)[0]  # strip ENCProdCat.xml
    return f"{base}/{cell_name}.zip"


def _unpack_enc_zip(zip_bytes: bytes, cell_name: str, cell_dir: Path) -> Path:
    """Extract {cell}.zip into `cell_dir`, return the path to `{cell}.000`.

    The zip is shaped `ENC_ROOT/<cell>/<cell>.000` plus update files.
    We flatten so the `.000` lands directly at `cell_dir/<cell>.000`,
    which is what plan/15 §Storage layout documents.
    """
    cell_dir.mkdir(parents=True, exist_ok=True)
    expected = f"{cell_name}.000"
    found: Path | None = None
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not name:
                continue
            # ENC update files are named {cell}.001, .002, .... Keep them
            # all for the preprocessor; just strip the ENC_ROOT/<cell>/
            # prefix so everything lives flat under `cell_dir`.
            target = cell_dir / name
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            if name == expected:
                found = target
    if found is None:
        raise ChartsFetchError(
            f"ENC zip for {cell_name} did not contain {expected}"
        )
    return found


async def _download_enc_cell(
    client: httpx.AsyncClient, cell: _CatalogCell, enc_root: Path
) -> EncCellFetchResult:
    """Download one ENC cell, or return a cached result if fresh.

    Per plan/15: cached cells whose `fetched_at` is within
    `BV_CHARTS_MAX_AGE_DAYS` are reused; we still return a result so the
    caller can feed everything to the preprocessor.
    """
    settings = get_settings()
    cell_dir = enc_root / cell.name
    s57_path = cell_dir / f"{cell.name}.000"
    fetched_at = _read_fetched_at(cell_dir)

    if s57_path.exists() and _is_fresh(fetched_at, settings.charts_max_age_days):
        assert fetched_at is not None
        return EncCellFetchResult(
            cell_id=cell.name,
            s57_path=s57_path,
            fetched_at=fetched_at,
            bytes_downloaded=0,
        )

    url = _enc_zip_url(cell.name)
    bbox_attr = ",".join(f"{v:.6f}" for v in cell.bbox)
    start = time.monotonic()
    with _tracer.start_as_current_span(
        "charts.fetch",
        attributes={
            "charts.source": "noaa_enc",
            "charts.cell": cell.name,
            "charts.bbox": bbox_attr,
        },
    ) as span:
        log.info("charts.fetch.start", source="noaa_enc", cell=cell.name, url=url)
        try:
            async for attempt in _retry_policy():
                with attempt:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    payload = resp.content
        except Exception as exc:
            raise ChartsFetchError(
                f"failed to fetch ENC cell {cell.name}: {exc}"
            ) from exc

        try:
            s57_path = _unpack_enc_zip(payload, cell.name, cell_dir)
        except zipfile.BadZipFile as exc:
            raise ChartsFetchError(
                f"ENC zip for {cell.name} is corrupt: {exc}"
            ) from exc

        now = _utc_now()
        _write_fetched_at(cell_dir, now)
        elapsed = time.monotonic() - start
        span.set_attribute("charts.bytes_downloaded", len(payload))
        span.set_attribute("charts.wallclock_seconds", elapsed)
        _fetch_bytes.add(len(payload), {"source": "noaa_enc"})
        _fetch_seconds.record(elapsed, {"source": "noaa_enc"})
        log.info(
            "charts.fetch.done",
            source="noaa_enc",
            cell=cell.name,
            bytes=len(payload),
            wallclock_s=elapsed,
        )
        return EncCellFetchResult(
            cell_id=cell.name,
            s57_path=s57_path,
            fetched_at=now,
            bytes_downloaded=len(payload),
        )


async def fetch_enc_cells(
    bbox: Bbox,
    out_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[EncCellFetchResult]:
    """Fetch every NOAA ENC cell whose panel bbox intersects `bbox`.

    Returns one `EncCellFetchResult` per intersecting cell (including
    those served from cache — the caller wants the full list). If the
    catalog parses but no cells intersect, returns an empty list; the
    caller decides whether that's a coverage gap based on whether OSM
    also comes up empty.

    Raises `ChartsFetchError` only if the catalog itself can't be
    fetched and no cached copy is on disk.
    """
    settings = get_settings()
    enc_root = out_dir / "enc"
    enc_root.mkdir(parents=True, exist_ok=True)
    catalog_path = out_dir / "catalog.xml"

    owned = client is None
    http = client if client is not None else _owned_client()
    try:
        xml_bytes = await _fetch_catalog_xml(
            http, catalog_path, max_age_days=settings.charts_max_age_days
        )
        all_cells = _parse_catalog(xml_bytes)
        intersecting = [c for c in all_cells if _bboxes_intersect(c.bbox, bbox)]
        log.info(
            "charts.catalog.parsed",
            total=len(all_cells),
            intersecting=len(intersecting),
        )
        results: list[EncCellFetchResult] = []
        for cell in intersecting:
            results.append(await _download_enc_cell(http, cell, enc_root))
        return results
    finally:
        if owned:
            await http.aclose()


# --------------------------------------------------------------------------- #
# OSM / Overpass: fetch_osm_extract
# --------------------------------------------------------------------------- #


def _overpass_query(bbox: Bbox) -> str:
    """The exact query documented in plan/15 §Chart fetching."""
    lat_min, lon_min, lat_max, lon_max = bbox
    bbox_str = f"{lat_min},{lon_min},{lat_max},{lon_max}"
    return (
        "[out:xml][timeout:180];\n"
        "(\n"
        f"  way[natural=coastline]({bbox_str});\n"
        f"  node[seamark:type]({bbox_str});\n"
        f"  way[seamark:type]({bbox_str});\n"
        f"  relation[seamark:type]({bbox_str});\n"
        ");\n"
        "(._;>;);\n"
        "out;\n"
    )


async def fetch_osm_extract(
    bbox: Bbox,
    out_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> OsmExtractFetchResult | None:
    """Fetch OSM features (coastline + seamarks) for `bbox` via Overpass.

    Caches `{bbox_hash}.osm` under `out_dir/osm/`; reuses the cached
    copy when `fetched_at.txt` in that dir is within
    `BV_CHARTS_MAX_AGE_DAYS`. Returns `None` only on a deliberate no-op
    (currently never — callers decide when to skip OSM). On upstream
    failure (after retries), raises `ChartsFetchError`.
    """
    settings = get_settings()
    osm_root = out_dir / "osm"
    osm_root.mkdir(parents=True, exist_ok=True)
    bbox_id = _bbox_hash(bbox)
    osm_path = osm_root / f"{bbox_id}.osm"
    meta_dir = osm_root / f".{bbox_id}"  # sibling for fetched_at.txt
    meta_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = _read_fetched_at(meta_dir)

    if osm_path.exists() and _is_fresh(fetched_at, settings.charts_max_age_days):
        assert fetched_at is not None
        return OsmExtractFetchResult(
            extract_id=bbox_id,
            pbf_path=osm_path,
            fetched_at=fetched_at,
            bytes_downloaded=0,
        )

    url = settings.overpass_base_url
    query = _overpass_query(bbox)
    bbox_attr = ",".join(f"{v:.6f}" for v in bbox)
    start = time.monotonic()

    owned = client is None
    http = client if client is not None else _owned_client()
    try:
        with _tracer.start_as_current_span(
            "charts.fetch",
            attributes={
                "charts.source": "osm",
                "charts.bbox": bbox_attr,
                "charts.extract_id": bbox_id,
            },
        ) as span:
            log.info(
                "charts.fetch.start",
                source="osm",
                extract_id=bbox_id,
                url=url,
            )
            try:
                async for attempt in _retry_policy():
                    with attempt:
                        resp = await http.post(url, data={"data": query})
                        resp.raise_for_status()
                        payload = resp.content
            except Exception as exc:
                raise ChartsFetchError(
                    f"failed to fetch OSM extract for bbox {bbox_attr}: {exc}"
                ) from exc

            osm_path.write_bytes(payload)
            now = _utc_now()
            _write_fetched_at(meta_dir, now)
            elapsed = time.monotonic() - start
            span.set_attribute("charts.bytes_downloaded", len(payload))
            span.set_attribute("charts.wallclock_seconds", elapsed)
            _fetch_bytes.add(len(payload), {"source": "osm"})
            _fetch_seconds.record(elapsed, {"source": "osm"})
            log.info(
                "charts.fetch.done",
                source="osm",
                extract_id=bbox_id,
                bytes=len(payload),
                wallclock_s=elapsed,
            )
            return OsmExtractFetchResult(
                extract_id=bbox_id,
                pbf_path=osm_path,
                fetched_at=now,
                bytes_downloaded=len(payload),
            )
    finally:
        if owned:
            await http.aclose()


# --------------------------------------------------------------------------- #
# GEBCO: locate_gebco_tile
# --------------------------------------------------------------------------- #


async def locate_gebco_tile(gebco_path: Path | None) -> Path:
    """Validate that the operator-supplied GEBCO netCDF path exists.

    GEBCO is ~8 GB — we never download it (plan/15 §Data sources). The
    operator supplies the path via `BV_GEBCO_PATH`; this helper just
    surfaces a clean `ChartsCoverageError` if it's missing so the
    voyage job can terminate with `CHARTS_NOT_AVAILABLE`.
    """
    if gebco_path is None:
        raise ChartsCoverageError("no BV_GEBCO_PATH configured")
    if not gebco_path.exists():
        raise ChartsCoverageError(f"GEBCO tile not found: {gebco_path}")
    return gebco_path


__all__ = [
    "Bbox",
    "ChartsCoverageError",
    "ChartsFetchError",
    "EncCellFetchResult",
    "OsmExtractFetchResult",
    "fetch_enc_cells",
    "fetch_osm_extract",
    "locate_gebco_tile",
]
