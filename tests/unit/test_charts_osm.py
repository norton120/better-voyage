"""OSM preprocessor tests.

OSM PBF fixtures are expensive to construct; the OSM XML format is
identical in semantics and osmium reads it transparently. Each test
writes a tiny `.osm` XML fixture into `tmp_path`, runs
`preprocess_osm_extract`, and inspects the resulting GeoJSON.

Fixture layout covers one of each feature kind the preprocessor lifts:
- Two coastline ways (non-closed) → two `land` LineString features.
- One `natural=land` closed way → one `land` Polygon feature.
- One `seamark:type=wreck` node → one `obstacle` feature.
- One `seamark:type=restricted_area` closed way → one `restricted`
  feature.
- Three navaid nodes covering the three main sym mappings.
- One feature placed outside the preprocessing bbox — must be dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest

from app.services.charts_osm import (
    OsmExtractMeta,
    _seamark_to_sym,
    preprocess_osm_extract,
)
from app.services.charts_schema import LAYER_KEY, NAVAID_SYMS

# Bbox used by all tests: Chesapeake-ish, lat 37..38, lon -76..-75.
BBOX = (37.0, -76.0, 38.0, -75.0)


FIXTURE_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <!-- Coastline way A: two nodes inside bbox, non-closed. -->
  <node id="1"  lat="37.10" lon="-75.90"/>
  <node id="2"  lat="37.20" lon="-75.80"/>
  <way id="101">
    <nd ref="1"/><nd ref="2"/>
    <tag k="natural" v="coastline"/>
  </way>

  <!-- Coastline way B: distinct from A, non-closed. -->
  <node id="3"  lat="37.50" lon="-75.50"/>
  <node id="4"  lat="37.55" lon="-75.40"/>
  <node id="5"  lat="37.60" lon="-75.30"/>
  <way id="102">
    <nd ref="3"/><nd ref="4"/><nd ref="5"/>
    <tag k="natural" v="coastline"/>
  </way>

  <!-- Wreck node (obstacle). -->
  <node id="10" lat="37.40" lon="-75.60">
    <tag k="seamark:type" v="wreck"/>
    <tag k="seamark:name" v="SS Testboat"/>
    <tag k="seamark:wreck:depth" v="8.5"/>
  </node>

  <!-- Restricted area: closed way with 5 nodes (4 corners + close). -->
  <node id="20" lat="37.70" lon="-75.70"/>
  <node id="21" lat="37.80" lon="-75.70"/>
  <node id="22" lat="37.80" lon="-75.60"/>
  <node id="23" lat="37.70" lon="-75.60"/>
  <way id="201">
    <nd ref="20"/><nd ref="21"/><nd ref="22"/><nd ref="23"/><nd ref="20"/>
    <tag k="seamark:type" v="restricted_area"/>
    <tag k="name" v="Test Restricted"/>
  </way>

  <!-- Navaid: red lateral buoy. -->
  <node id="30" lat="37.30" lon="-75.70">
    <tag k="seamark:type" v="buoy_lateral"/>
    <tag k="seamark:buoy_lateral:colour" v="red"/>
    <tag k="seamark:name" v="R '4'"/>
  </node>

  <!-- Navaid: green lateral beacon. -->
  <node id="31" lat="37.35" lon="-75.65">
    <tag k="seamark:type" v="beacon_lateral"/>
    <tag k="seamark:beacon_lateral:colour" v="green"/>
    <tag k="seamark:name" v="G '5'"/>
  </node>

  <!-- Navaid: minor light. -->
  <node id="32" lat="37.45" lon="-75.55">
    <tag k="seamark:type" v="light_minor"/>
    <tag k="seamark:name" v="Test Point Light"/>
    <tag k="seamark:light:character" v="Fl"/>
    <tag k="seamark:light:period" v="4"/>
    <tag k="seamark:light:colour" v="white"/>
  </node>

  <!-- OUTSIDE bbox: coastline way at lat ~40. Must be filtered out. -->
  <node id="40" lat="40.10" lon="-75.10"/>
  <node id="41" lat="40.20" lon="-75.20"/>
  <way id="301">
    <nd ref="40"/><nd ref="41"/>
    <tag k="natural" v="coastline"/>
  </way>

  <!-- OUTSIDE bbox: wreck node south of bbox. Must be filtered out. -->
  <node id="42" lat="30.0" lon="-75.0">
    <tag k="seamark:type" v="wreck"/>
  </node>
</osm>
"""


@pytest.fixture()
def fixture_osm(tmp_path: Path) -> Path:
    """Write the shared OSM XML fixture and return its path."""
    p = tmp_path / "chesapeake.osm"
    p.write_text(FIXTURE_XML)
    return p


def _load_output(out_path: Path) -> dict:
    with out_path.open() as fp:
        return json.load(fp)


def test_preprocess_writes_geojson_with_all_layers(
    fixture_osm: Path, tmp_path: Path
) -> None:
    out = tmp_path / "chesapeake.preprocessed.geojson"
    meta = preprocess_osm_extract(fixture_osm, BBOX, out)
    assert isinstance(meta, OsmExtractMeta)
    assert meta.extract_id == "chesapeake"
    assert meta.bbox == BBOX

    data = _load_output(out)
    assert data["type"] == "FeatureCollection"
    # GeoJSON bbox convention is lon/lat not lat/lon.
    assert data["bbox"] == [-76.0, 37.0, -75.0, 38.0]


def test_feature_counts_match_fixture(fixture_osm: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.geojson"
    meta = preprocess_osm_extract(fixture_osm, BBOX, out)
    # Two coastline ways inside bbox.
    assert meta.feature_counts["land"] == 2
    # One wreck node inside bbox; the one outside must have been dropped.
    assert meta.feature_counts["obstacle"] == 1
    # One restricted polygon inside bbox.
    assert meta.feature_counts["restricted"] == 1
    # Three navaids.
    assert meta.feature_counts["navaid"] == 3
    # No shallow — OSM has no DEPARE equivalent.
    assert "shallow" not in meta.feature_counts


def test_navaid_syms_match_opencpn_palette(
    fixture_osm: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.geojson"
    preprocess_osm_extract(fixture_osm, BBOX, out)
    data = _load_output(out)
    navaids = [
        f for f in data["features"] if f["properties"][LAYER_KEY] == "navaid"
    ]
    syms = {f["properties"]["sym"] for f in navaids}
    assert syms == {"Buoy, Red", "Beacon, Green", "Light"}
    for f in navaids:
        assert f["properties"]["sym"] in NAVAID_SYMS


def test_wreck_passes_through_depth(fixture_osm: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.geojson"
    preprocess_osm_extract(fixture_osm, BBOX, out)
    data = _load_output(out)
    wrecks = [
        f for f in data["features"] if f["properties"][LAYER_KEY] == "obstacle"
    ]
    assert len(wrecks) == 1
    assert wrecks[0]["properties"]["clearance_m"] == pytest.approx(8.5)
    assert wrecks[0]["properties"]["name"] == "SS Testboat"


def test_bbox_filters_out_distant_features(
    fixture_osm: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.geojson"
    preprocess_osm_extract(fixture_osm, BBOX, out)
    data = _load_output(out)
    # Any feature referencing osm_id of the outside coastline way (301)
    # or outside wreck (42) must be absent.
    ids = {f["properties"].get("osm_id") for f in data["features"]}
    assert "way/301" not in ids
    assert "node/42" not in ids


def test_output_roundtrips_through_geopandas(
    fixture_osm: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.geojson"
    preprocess_osm_extract(fixture_osm, BBOX, out)
    gdf = gpd.read_file(out)
    assert len(gdf) > 0
    assert LAYER_KEY in gdf.columns
    assert set(gdf[LAYER_KEY].unique()) <= {
        "land", "shallow", "obstacle", "restricted", "navaid"
    }


def test_restricted_polygon_is_valid(fixture_osm: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.geojson"
    preprocess_osm_extract(fixture_osm, BBOX, out)
    data = _load_output(out)
    restricted = [
        f for f in data["features"] if f["properties"][LAYER_KEY] == "restricted"
    ]
    assert len(restricted) == 1
    assert restricted[0]["geometry"]["type"] == "Polygon"
    # Rough sanity: 4 corner ring + close node.
    ring = restricted[0]["geometry"]["coordinates"][0]
    assert len(ring) == 5
    assert ring[0] == ring[-1]


def test_coastline_is_linestring(fixture_osm: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.geojson"
    preprocess_osm_extract(fixture_osm, BBOX, out)
    data = _load_output(out)
    lands = [
        f for f in data["features"] if f["properties"][LAYER_KEY] == "land"
    ]
    # Both coastline ways are LineStrings (not closed, not polygonized).
    assert len(lands) == 2
    for f in lands:
        assert f["geometry"]["type"] == "LineString"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        preprocess_osm_extract(
            tmp_path / "does-not-exist.osm",
            BBOX,
            tmp_path / "out.geojson",
        )


def test_geojson_is_compact(fixture_osm: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.geojson"
    preprocess_osm_extract(fixture_osm, BBOX, out)
    text = out.read_text()
    # `json.dump(..., separators=(",",":"))` collapses structural
    # whitespace. Literal commas/colons inside string values (e.g.
    # "Buoy, Red") are fine, so we check that the file has no newlines
    # and no indentation spaces between structural tokens.
    assert "\n" not in text
    assert '", "' not in text   # no space-separated object keys.
    assert "\": " not in text   # no space after colon in keys.


# --- _seamark_to_sym unit coverage (doesn't need a PBF) --------------------


def test_seamark_to_sym_red_buoy() -> None:
    assert _seamark_to_sym(
        {
            "seamark:type": "buoy_lateral",
            "seamark:buoy_lateral:colour": "red",
        }
    ) == "Buoy, Red"


def test_seamark_to_sym_green_buoy() -> None:
    assert _seamark_to_sym(
        {
            "seamark:type": "buoy_lateral",
            "seamark:buoy_lateral:colour": "green",
        }
    ) == "Buoy, Green"


def test_seamark_to_sym_cardinal_falls_to_yellow() -> None:
    assert _seamark_to_sym(
        {
            "seamark:type": "buoy_cardinal",
            "seamark:buoy_cardinal:category": "north",
        }
    ) == "Buoy, Yellow"


def test_seamark_to_sym_beacon_red() -> None:
    assert _seamark_to_sym(
        {
            "seamark:type": "beacon_lateral",
            "seamark:beacon_lateral:colour": "red",
        }
    ) == "Beacon, Red"


def test_seamark_to_sym_beacon_unknown_colour_to_white() -> None:
    assert _seamark_to_sym(
        {"seamark:type": "beacon_lateral"}
    ) == "Beacon, White"


def test_seamark_to_sym_light_minor() -> None:
    assert _seamark_to_sym({"seamark:type": "light_minor"}) == "Light"


def test_seamark_to_sym_light_major() -> None:
    assert _seamark_to_sym({"seamark:type": "light_major"}) == "Light"


def test_seamark_to_sym_unknown_falls_back() -> None:
    assert _seamark_to_sym({"seamark:type": "something_weird"}) == "Waypoint"


def test_seamark_to_sym_always_in_navaid_palette() -> None:
    for tag_val in (
        "buoy_lateral",
        "buoy_cardinal",
        "beacon_lateral",
        "light_minor",
        "light_major",
        "unrecognised_category",
    ):
        sym = _seamark_to_sym({"seamark:type": tag_val})
        assert sym in NAVAID_SYMS
