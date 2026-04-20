"""Chart fetcher tests — NOAA ENC catalog + zip unpack, Overpass, GEBCO.

Everything is mock-based via `pytest-httpx`; no real network. The
catalog XML and ENC zip are built in-memory so we can assert exact
intersect + unpack behaviour.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest

from app.services.charts_fetch import (
    ChartsCoverageError,
    ChartsFetchError,
    OsmExtractFetchResult,
    fetch_enc_cells,
    fetch_osm_extract,
    locate_gebco_tile,
)

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _catalog_xml() -> bytes:
    """Three cells: one intersects our test bbox, two do not.

    Test bbox: (37.5, -77.0, 39.0, -75.5) — the Chesapeake box used in
    the GEBCO tests.

    - US4CHES : (38.0, -76.5, 38.8, -76.0)   intersects
    - US4FLAX : (25.0, -81.0, 26.0, -80.0)   far south
    - US4NYNY : (40.5, -74.5, 41.0, -74.0)   north of bbox
    """
    def cell(name: str, vertices: list[tuple[float, float]]) -> bytes:
        verts = b"".join(
            f"<vertex><lat>{lat}</lat><long>{lon}</long></vertex>".encode()
            for lat, lon in vertices
        )
        return (
            b"<cell><name>" + name.encode() + b"</name>"
            b"<cov><panel>" + verts + b"</panel></cov></cell>"
        )

    return (
        b"<?xml version='1.0' encoding='UTF-8'?>"
        b"<EncProductCatalog>"
        + cell("US4CHES", [(38.0, -76.5), (38.0, -76.0), (38.8, -76.0), (38.8, -76.5)])
        + cell("US4FLAX", [(25.0, -81.0), (25.0, -80.0), (26.0, -80.0), (26.0, -81.0)])
        + cell("US4NYNY", [(40.5, -74.5), (40.5, -74.0), (41.0, -74.0), (41.0, -74.5)])
        + b"</EncProductCatalog>"
    )


def _enc_zip_bytes(cell_name: str = "US4CHES") -> bytes:
    """Build an in-memory zip shaped like a real NOAA ENC archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"ENC_ROOT/{cell_name}/{cell_name}.000", b"S57-bytes-go-here")
        zf.writestr(f"ENC_ROOT/{cell_name}/{cell_name}.001", b"update-file")
    return buf.getvalue()


TEST_BBOX = (37.5, -77.0, 39.0, -75.5)


def _arm_catalog(httpx_mock, *, body: bytes | None = None) -> None:
    httpx_mock.add_response(
        url="https://charts.noaa.gov/ENCs/ENCProdCat.xml",
        content=body if body is not None else _catalog_xml(),
    )


# --------------------------------------------------------------------------- #
# ENC tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_catalog_parse_filters_to_intersecting_cells(
    tmp_path: Path, httpx_mock
) -> None:
    _arm_catalog(httpx_mock)
    # Only US4CHES should be fetched.
    httpx_mock.add_response(
        url="https://charts.noaa.gov/ENCs/US4CHES.zip",
        content=_enc_zip_bytes("US4CHES"),
    )

    results = await fetch_enc_cells(TEST_BBOX, tmp_path)

    assert [r.cell_id for r in results] == ["US4CHES"]
    # Exactly two requests: the catalog + the one intersecting cell.
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    urls = {str(r.url) for r in requests}
    assert "https://charts.noaa.gov/ENCs/US4CHES.zip" in urls
    assert not any("US4FLAX" in u or "US4NYNY" in u for u in urls)


@pytest.mark.asyncio
async def test_enc_zip_is_unpacked_and_fetched_at_recorded(
    tmp_path: Path, httpx_mock
) -> None:
    _arm_catalog(httpx_mock)
    httpx_mock.add_response(
        url="https://charts.noaa.gov/ENCs/US4CHES.zip",
        content=_enc_zip_bytes("US4CHES"),
    )

    results = await fetch_enc_cells(TEST_BBOX, tmp_path)

    assert len(results) == 1
    r = results[0]
    assert r.s57_path == tmp_path / "enc" / "US4CHES" / "US4CHES.000"
    assert r.s57_path.exists()
    assert r.s57_path.read_bytes() == b"S57-bytes-go-here"
    # Update file was also extracted.
    assert (tmp_path / "enc" / "US4CHES" / "US4CHES.001").exists()
    # fetched_at.txt is a parseable ISO timestamp.
    stamp_file = tmp_path / "enc" / "US4CHES" / "fetched_at.txt"
    assert stamp_file.exists()
    from datetime import datetime
    datetime.fromisoformat(stamp_file.read_text().strip())  # no-raise
    assert r.bytes_downloaded > 0


@pytest.mark.asyncio
async def test_enc_cache_hit_skips_second_http(
    tmp_path: Path, httpx_mock
) -> None:
    _arm_catalog(httpx_mock)
    httpx_mock.add_response(
        url="https://charts.noaa.gov/ENCs/US4CHES.zip",
        content=_enc_zip_bytes("US4CHES"),
    )
    first = await fetch_enc_cells(TEST_BBOX, tmp_path)
    assert len(first) == 1
    first_count = len(httpx_mock.get_requests())

    # Second call: catalog.xml is fresh on disk AND the cell is unpacked
    # + within max_age_days, so we expect zero new HTTP requests.
    second = await fetch_enc_cells(TEST_BBOX, tmp_path)

    assert len(second) == 1
    assert second[0].s57_path == first[0].s57_path
    assert second[0].bytes_downloaded == 0
    assert len(httpx_mock.get_requests()) == first_count


@pytest.mark.asyncio
async def test_no_intersecting_cells_returns_empty_list(
    tmp_path: Path, httpx_mock
) -> None:
    _arm_catalog(httpx_mock)
    # Bbox in the middle of the Pacific — no cell in the synthetic catalog matches.
    pacific_bbox = (0.0, -140.0, 1.0, -139.0)

    results = await fetch_enc_cells(pacific_bbox, tmp_path)

    assert results == []
    # Only the catalog request — no zip requests.
    assert len(httpx_mock.get_requests()) == 1


# --------------------------------------------------------------------------- #
# Overpass tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_overpass_success_writes_osm_file(
    tmp_path: Path, httpx_mock
) -> None:
    httpx_mock.add_response(
        url="https://overpass-api.de/api/interpreter",
        method="POST",
        content=b"<osm version='0.6'></osm>",
    )

    result = await fetch_osm_extract(TEST_BBOX, tmp_path)

    assert isinstance(result, OsmExtractFetchResult)
    assert result.pbf_path.exists()
    assert result.pbf_path.suffix == ".osm"
    assert result.pbf_path.read_bytes() == b"<osm version='0.6'></osm>"
    assert result.bytes_downloaded > 0
    # One request — a POST to Overpass.
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].method == "POST"
    # The query body is form-urlencoded `data=<query>`; decode before checking.
    from urllib.parse import parse_qs
    body = parse_qs(requests[0].content.decode())
    query = body["data"][0]
    assert "natural=coastline" in query
    assert "seamark:type" in query
    assert "37.5" in query and "-77.0" in query


@pytest.mark.asyncio
async def test_overpass_cache_hit_skips_second_http(
    tmp_path: Path, httpx_mock
) -> None:
    httpx_mock.add_response(
        url="https://overpass-api.de/api/interpreter",
        method="POST",
        content=b"<osm></osm>",
    )
    first = await fetch_osm_extract(TEST_BBOX, tmp_path)
    assert first is not None
    count_after_first = len(httpx_mock.get_requests())

    second = await fetch_osm_extract(TEST_BBOX, tmp_path)

    assert second is not None
    assert second.pbf_path == first.pbf_path
    assert second.bytes_downloaded == 0
    assert len(httpx_mock.get_requests()) == count_after_first


@pytest.mark.asyncio
async def test_overpass_5xx_raises_charts_fetch_error(
    tmp_path: Path, httpx_mock
) -> None:
    # All four mirrors (primary + 3 fallbacks from default config) return
    # 500 across tenacity's 3 retries = 12 attempts total before we give up.
    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.openstreetmap.ru/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
    ]
    for url in mirrors:
        for _ in range(3):
            httpx_mock.add_response(
                url=url, method="POST", status_code=500, content=b"boom",
            )

    with pytest.raises(ChartsFetchError):
        await fetch_osm_extract(TEST_BBOX, tmp_path)

    assert len(httpx_mock.get_requests()) == 3 * len(mirrors)


@pytest.mark.asyncio
async def test_overpass_falls_back_to_mirror_on_primary_5xx(
    tmp_path: Path, httpx_mock
) -> None:
    """When the primary Overpass host 504s, the next mirror takes over."""
    # Primary: 3 failing attempts.
    for _ in range(3):
        httpx_mock.add_response(
            url="https://overpass-api.de/api/interpreter",
            method="POST", status_code=504, content=b"timeout",
        )
    # First fallback mirror: success on the first try.
    httpx_mock.add_response(
        url="https://overpass.kumi.systems/api/interpreter",
        method="POST", status_code=200,
        content=b"<?xml version='1.0' encoding='UTF-8'?><osm/>",
    )

    result = await fetch_osm_extract(TEST_BBOX, tmp_path)
    assert result is not None
    # 3 failures on primary + 1 success on mirror = 4 requests.
    assert len(httpx_mock.get_requests()) == 4


# --------------------------------------------------------------------------- #
# GEBCO tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_locate_gebco_none_raises() -> None:
    with pytest.raises(ChartsCoverageError, match="no BV_GEBCO_PATH"):
        await locate_gebco_tile(None)


@pytest.mark.asyncio
async def test_locate_gebco_missing_raises(tmp_path: Path) -> None:
    missing = tmp_path / "no-such.nc"
    with pytest.raises(ChartsCoverageError, match="not found"):
        await locate_gebco_tile(missing)


@pytest.mark.asyncio
async def test_locate_gebco_existing_returns_path(tmp_path: Path) -> None:
    p = tmp_path / "gebco.nc"
    p.write_bytes(b"\x00")
    out = await locate_gebco_tile(p)
    assert out == p


# --------------------------------------------------------------------------- #
# unrelated sanity: regex-based URL matching still works for the catalog
# (guards against future changes to BV_NOAA_ENC_CATALOG_URL styling)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_catalog_url_uses_settings(
    tmp_path: Path, httpx_mock
) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://charts\.noaa\.gov/ENCs/ENCProdCat\.xml"),
        content=_catalog_xml(),
    )
    httpx_mock.add_response(
        url=re.compile(r"https://charts\.noaa\.gov/ENCs/US4CHES\.zip"),
        content=_enc_zip_bytes("US4CHES"),
    )
    results = await fetch_enc_cells(TEST_BBOX, tmp_path)
    assert len(results) == 1
