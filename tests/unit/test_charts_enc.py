"""NOAA ENC preprocessor tests.

Building a real S-57 `.000` file in unit-test scope is painful —
the format is binary, uses IHO ISO 8211 framing, and GDAL's S57
driver expects a very specific layout. Instead we monkeypatch
`pyogrio.read_dataframe` to return synthetic `GeoDataFrame` per
layer, then exercise the preprocessor end-to-end.

Each test reasons about one S-57 → `bv:layer` mapping. A
round-trip test re-loads the written GeoJSON and asserts every
expected `bv:layer` tag is present.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from app.services import charts_enc
from app.services.charts_enc import EncCellMeta, preprocess_enc_cell
from app.services.charts_schema import LAYER_KEY

# ---------------------------------------------------------------------
# Import-time env var assertion — catches regressions in module init.


def test_ogr_s57_options_set_on_import() -> None:
    # Importing `app.services.charts_enc` at the top of the file
    # should have set the S-57 driver knobs we rely on.
    assert "OGR_S57_OPTIONS" in os.environ
    assert "UPDATES=APPLY" in os.environ["OGR_S57_OPTIONS"]
    assert "SPLIT_MULTIPOINT=ON" in os.environ["OGR_S57_OPTIONS"]
    assert "LIST_AS_STRING=ON" in os.environ["OGR_S57_OPTIONS"]


# ---------------------------------------------------------------------
# Fake pyogrio infrastructure


def _gdf(rows: list[dict[str, Any]]) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame from row dicts that include `geometry`."""
    if not rows:
        return gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _make_fake_reader(
    layers: dict[str, gpd.GeoDataFrame],
    missing: set[str] | None = None,
) -> Callable[..., gpd.GeoDataFrame]:
    """Return a fake `pyogrio.read_dataframe` keyed by layer name.

    Layers in `missing` raise `DataSourceError` (ENC cell doesn't
    ship that layer). Layers absent from both maps return an empty
    GeoDataFrame. Layers in `layers` return the provided df.
    """
    from pyogrio.errors import DataSourceError

    absent: set[str] = missing or set()

    def _read(path: Path, layer: str, **_: Any) -> gpd.GeoDataFrame:
        if layer in absent:
            raise DataSourceError(f"layer {layer} missing in fixture")
        if layer in layers:
            return layers[layer]
        return _gdf([])

    return _read


@pytest.fixture()
def fake_cell_path(tmp_path: Path) -> Path:
    """Create a dummy `.000` file on disk — we don't actually read it."""
    p = tmp_path / "US4MD01M" / "US4MD01M.000"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\x00")  # content irrelevant; pyogrio is faked.
    return p


# ---------------------------------------------------------------------
# Fixtures: synthetic layer dataframes


def _synthetic_layers() -> dict[str, gpd.GeoDataFrame]:
    """Full happy-path layer set covering every `bv:layer`."""
    # LAND: two overlapping polygons that should union into one
    # MultiPolygon (actually a single Polygon since they merge).
    land = _gdf(
        [
            {"geometry": Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])},
            {"geometry": Polygon([(1, 1), (3, 1), (3, 3), (1, 3)])},
        ]
    )

    # DEPARE: two shallow, one deep.
    depare = _gdf(
        [
            {
                "geometry": Polygon([(10, 10), (11, 10), (11, 11), (10, 11)]),
                "DRVAL1": 1.0,  # shallow (< 2.0 cutoff)
                "DRVAL2": 2.0,
            },
            {
                "geometry": Polygon([(11, 11), (12, 11), (12, 12), (11, 12)]),
                "DRVAL1": 0.5,  # shallow
                "DRVAL2": 1.5,
            },
            {
                "geometry": Polygon([(12, 12), (13, 12), (13, 13), (12, 13)]),
                "DRVAL1": 5.0,  # deep — dropped
                "DRVAL2": 10.0,
            },
        ]
    )

    # Obstacles: one with surveyed clearance, one unsurveyed.
    obstrn = _gdf(
        [
            {
                "geometry": Point(20.0, 20.0),
                "VALSOU": 3.5,
                "OBJNAM": "Wreck A",
            },
        ]
    )
    wrecks = _gdf(
        [
            {
                "geometry": Point(20.5, 20.5),
                "VALSOU": None,  # unsurveyed
                "OBJNAM": "Wreck B",
            },
        ]
    )
    uwtroc = _gdf(
        [
            {
                "geometry": Point(21.0, 21.0),
                "VALSOU": 1.2,
                "OBJNAM": None,
            },
        ]
    )

    # Restricted: three layers that union into a single polygon.
    resare = _gdf(
        [{"geometry": Polygon([(30, 30), (31, 30), (31, 31), (30, 31)])}]
    )
    ctnare = _gdf(
        [{"geometry": Polygon([(31, 31), (32, 31), (32, 32), (31, 32)])}]
    )
    marcul = _gdf(
        [{"geometry": Polygon([(32, 32), (33, 32), (33, 33), (32, 33)])}]
    )

    # Navaids: red lateral buoy, green beacon, light, white safe-water buoy,
    # buoy with unknown color, bridge.
    boylat = _gdf(
        [
            {
                "geometry": Point(40.0, 40.0),
                "COLOUR": 3,  # red
                "OBJNAM": "R '4'",
            },
            {
                "geometry": Point(40.1, 40.1),
                "COLOUR": 99,  # unknown
                "OBJNAM": None,
            },
        ]
    )
    boysaw = _gdf(
        [
            {
                "geometry": Point(40.5, 40.5),
                "COLOUR": 1,  # white
                "OBJNAM": None,
            }
        ]
    )
    bcnlat = _gdf(
        [
            {
                "geometry": Point(40.2, 40.2),
                "COLOUR": 4,  # green
                "OBJNAM": None,
            }
        ]
    )
    lights = _gdf(
        [
            {
                "geometry": Point(40.3, 40.3),
                "OBJNAM": "Fl R 2s",
                "LITCHR": 2,
                "SIGPER": 2,
            }
        ]
    )
    bridge = _gdf(
        [
            {
                "geometry": Point(40.4, 40.4),
                "OBJNAM": "US-50 Bridge",
                "VERCLR": 20.0,
            }
        ]
    )

    return {
        "LNDARE": land,
        "DEPARE": depare,
        "OBSTRN": obstrn,
        "WRECKS": wrecks,
        "UWTROC": uwtroc,
        "RESARE": resare,
        "CTNARE": ctnare,
        "MARCUL": marcul,
        "BOYLAT": boylat,
        "BOYSAW": boysaw,
        "BCNLAT": bcnlat,
        "LIGHTS": lights,
        "BRIDGE": bridge,
    }


# ---------------------------------------------------------------------
# Happy-path end-to-end


def test_preprocess_full_cell_end_to_end(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layers = _synthetic_layers()
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader(layers),
    )

    out_path = tmp_path / "US4MD01M.preprocessed.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out_path)

    assert isinstance(meta, EncCellMeta)
    assert meta.cell_id == "US4MD01M"
    # All five layer tags populated.
    assert meta.feature_counts["land"] == 1
    assert meta.feature_counts["shallow"] == 2
    assert meta.feature_counts["obstacle"] == 3
    assert meta.feature_counts["restricted"] == 1
    # navaids: 2 BOYLAT + 1 BOYSAW + 1 BCNLAT + 1 LIGHTS + 1 BRIDGE = 6
    assert meta.feature_counts["navaid"] == 6

    # Output file exists and is valid GeoJSON.
    assert out_path.exists()
    doc = json.loads(out_path.read_text())
    assert doc["type"] == "FeatureCollection"
    assert doc["bv:cell_id"] == "US4MD01M"
    assert len(doc["bbox"]) == 4  # [lon_min, lat_min, lon_max, lat_max]

    # feature_counts sums to total features written.
    total = sum(meta.feature_counts.values())
    assert len(doc["features"]) == total


# ---------------------------------------------------------------------
# Round-trip — re-load with geopandas and verify tags


def test_round_trip_bv_layer_tags(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layers = _synthetic_layers()
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader(layers),
    )

    out_path = tmp_path / "cell.geojson"
    preprocess_enc_cell(fake_cell_path, out_path)

    # Re-load as JSON (geopandas.read_file on some GDAL builds does not
    # preserve the `bv:layer` namespaced property name cleanly).
    doc = json.loads(out_path.read_text())
    tags = [f["properties"][LAYER_KEY] for f in doc["features"]]
    assert "land" in tags
    assert "shallow" in tags
    assert "obstacle" in tags
    assert "restricted" in tags
    assert "navaid" in tags


# ---------------------------------------------------------------------
# Land: LNDARE polygons union into one MultiPolygon (or Polygon).


def test_land_unions_polygons(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    land = _gdf(
        [
            {"geometry": Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])},
            # Disjoint second polygon — union should yield a MultiPolygon.
            {"geometry": Polygon([(5, 5), (6, 5), (6, 6), (5, 6)])},
        ]
    )
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader({"LNDARE": land}),
    )
    out = tmp_path / "land.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out)

    assert meta.feature_counts["land"] == 1
    doc = json.loads(out.read_text())
    land_features = [
        f for f in doc["features"] if f["properties"][LAYER_KEY] == "land"
    ]
    assert len(land_features) == 1
    assert land_features[0]["geometry"]["type"] == "MultiPolygon"


def test_land_falls_back_to_coalne_when_lndare_missing(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coalne = _gdf([{"geometry": LineString([(0, 0), (1, 1), (2, 0)])}])
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader(
            {"COALNE": coalne}, missing={"LNDARE"}
        ),
    )
    out = tmp_path / "coalne.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out)
    assert meta.feature_counts["land"] == 1


# ---------------------------------------------------------------------
# Shallow DRVAL1 filter


def test_shallow_filter_respects_cutoff(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depare = _gdf(
        [
            {
                "geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                "DRVAL1": 1.0,  # kept
                "DRVAL2": 2.0,
            },
            {
                "geometry": Polygon([(2, 2), (3, 2), (3, 3), (2, 3)]),
                "DRVAL1": 5.0,  # dropped
                "DRVAL2": 10.0,
            },
        ]
    )
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader({"DEPARE": depare}),
    )
    out = tmp_path / "shallow.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out)

    assert meta.feature_counts["shallow"] == 1
    doc = json.loads(out.read_text())
    shallow_features = [
        f for f in doc["features"] if f["properties"][LAYER_KEY] == "shallow"
    ]
    assert len(shallow_features) == 1
    assert shallow_features[0]["properties"]["drval1_m"] == 1.0
    assert shallow_features[0]["properties"]["drval2_m"] == 2.0


# ---------------------------------------------------------------------
# Obstacles: one feature each, clearance_m preserved


def test_obstacles_preserve_valsou_as_clearance(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obstrn = _gdf(
        [
            {
                "geometry": Point(0.0, 0.0),
                "VALSOU": 3.5,
                "OBJNAM": "Rock A",
            },
            {
                "geometry": Point(1.0, 1.0),
                "VALSOU": None,
                "OBJNAM": "Rock B",
            },
        ]
    )
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader({"OBSTRN": obstrn}),
    )
    out = tmp_path / "obs.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out)

    assert meta.feature_counts["obstacle"] == 2
    doc = json.loads(out.read_text())
    obs = [
        f for f in doc["features"] if f["properties"][LAYER_KEY] == "obstacle"
    ]
    assert len(obs) == 2
    clearances = sorted(
        (f["properties"]["clearance_m"] for f in obs),
        key=lambda v: (v is None, v),
    )
    assert clearances[0] == 3.5
    assert clearances[1] is None


# ---------------------------------------------------------------------
# Restricted: RESARE + MARCUL union into one feature; CTNARE/DRGARE excluded


def test_restricted_unions_resare_and_marcul_and_ignores_ctnare_drgare(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resare = _gdf(
        [{"geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])}]
    )
    marcul = _gdf(
        [{"geometry": Polygon([(10, 10), (11, 10), (11, 11), (10, 11)])}]
    )
    # CTNARE (caution-area, advisory only) + DRGARE (dredged channel,
    # navigable) must NOT be classified as restricted — they routinely
    # span huge offshore regions in small-scale cells and would block
    # every route planned through US waters.
    ctnare = _gdf(
        [{"geometry": Polygon([(5, 5), (6, 5), (6, 6), (5, 6)])}]
    )
    drgare = _gdf(
        [{"geometry": Polygon([(20, 20), (21, 20), (21, 21), (20, 21)])}]
    )
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader(
            {
                "RESARE": resare, "MARCUL": marcul,
                "CTNARE": ctnare, "DRGARE": drgare,
            }
        ),
    )
    out = tmp_path / "restricted.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out)

    assert meta.feature_counts["restricted"] == 1
    doc = json.loads(out.read_text())
    rests = [
        f for f in doc["features"] if f["properties"][LAYER_KEY] == "restricted"
    ]
    assert len(rests) == 1
    assert rests[0]["geometry"]["type"] == "MultiPolygon"
    # The restricted bounds should cover RESARE + MARCUL only
    # (roughly lon 0..11, lat 0..11), NOT the CTNARE (5..6) or DRGARE (20..21).
    from shapely.geometry import shape
    g = shape(rests[0]["geometry"])
    assert g.bounds[2] <= 11.0, f"DRGARE leaked in: bounds={g.bounds}"


# ---------------------------------------------------------------------
# Navaid symbol mapping


def test_navaid_symbol_mapping(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boylat = _gdf(
        [
            # Red lateral buoy with a name.
            {
                "geometry": Point(0.0, 0.0),
                "COLOUR": 3,
                "OBJNAM": "R '4'",
            },
            # Unknown color → Waypoint, synthesized name.
            {
                "geometry": Point(1.0, 1.0),
                "COLOUR": 99,
                "OBJNAM": None,
            },
        ]
    )
    bcnlat = _gdf(
        [
            {
                "geometry": Point(2.0, 2.0),
                "COLOUR": 4,
                "OBJNAM": None,
            }
        ]
    )
    lights = _gdf([{"geometry": Point(3.0, 3.0), "OBJNAM": "Fl R 2s"}])

    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader(
            {"BOYLAT": boylat, "BCNLAT": bcnlat, "LIGHTS": lights}
        ),
    )
    out = tmp_path / "navaid.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out)

    assert meta.feature_counts["navaid"] == 4
    doc = json.loads(out.read_text())
    navaids = [
        f for f in doc["features"] if f["properties"][LAYER_KEY] == "navaid"
    ]
    # Map by layer for easy assertions.
    by_sym = {
        (f["properties"]["s57_layer"], f["properties"]["sym"]): f
        for f in navaids
    }
    # Red BOYLAT named R '4'.
    red_buoy = by_sym[("BOYLAT", "Buoy, Red")]
    assert red_buoy["properties"]["name"] == "R '4'"
    # Unknown-color BOYLAT → Waypoint fallback.
    assert ("BOYLAT", "Waypoint") in by_sym
    # Green BCNLAT → Beacon, Green.
    assert ("BCNLAT", "Beacon, Green") in by_sym
    # LIGHTS → Light.
    light = by_sym[("LIGHTS", "Light")]
    assert light["properties"]["name"] == "Fl R 2s"


# ---------------------------------------------------------------------
# Missing layers — pyogrio raises, we keep going


def test_missing_layer_is_skipped_cleanly(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only LNDARE is present; every other layer raises.
    land = _gdf([{"geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])}])
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader(
            {"LNDARE": land},
            missing={
                "DEPARE",
                "OBSTRN",
                "WRECKS",
                "UWTROC",
                "RESARE",
                "CTNARE",
                "MARCUL",
                "DRGARE",
                "BOYLAT",
                "BOYSAW",
                "BCNLAT",
                "LIGHTS",
                "BRIDGE",
                "COALNE",
            },
        ),
    )
    out = tmp_path / "sparse.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out)

    assert meta.feature_counts["land"] == 1
    assert meta.feature_counts["shallow"] == 0
    assert meta.feature_counts["obstacle"] == 0
    assert meta.feature_counts["restricted"] == 0
    assert meta.feature_counts["navaid"] == 0

    doc = json.loads(out.read_text())
    assert len(doc["features"]) == 1


# ---------------------------------------------------------------------
# Missing `.000` file — the one error we do raise


def test_missing_s57_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        preprocess_enc_cell(
            tmp_path / "nope.000", tmp_path / "out.geojson"
        )


# ---------------------------------------------------------------------
# feature_counts sum matches features written


def test_feature_counts_sum_matches_features(
    fake_cell_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layers = _synthetic_layers()
    monkeypatch.setattr(
        charts_enc.pyogrio,
        "read_dataframe",
        _make_fake_reader(layers),
    )
    out = tmp_path / "counts.geojson"
    meta = preprocess_enc_cell(fake_cell_path, out)

    doc = json.loads(out.read_text())
    assert sum(meta.feature_counts.values()) == len(doc["features"])
