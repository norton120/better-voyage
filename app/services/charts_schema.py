"""Shared schema for preprocessed chart layers.

Both the ENC preprocessor (plan/15 §Preprocessing, `services/charts_enc.py`)
and the OSM preprocessor (`services/charts_osm.py`) emit a single
GeoJSON FeatureCollection per source cell / extract. The ChartStore
then unions them into in-memory STRtrees keyed by `bv:layer`.

The layer names below are the only values allowed in each feature's
`properties["bv:layer"]` field. Keep the set narrow — every new layer
costs a query entry point on ChartStore.
"""

from __future__ import annotations

from typing import Literal

# One of these five tags lives on `properties["bv:layer"]` in the
# preprocessed GeoJSON. Callers should treat any other value as invalid
# input (we skip it with a warn).
Layer = Literal["land", "shallow", "obstacle", "restricted", "navaid"]

LAYERS: tuple[Layer, ...] = ("land", "shallow", "obstacle", "restricted", "navaid")

# The JSON-level key that carries the layer tag. Namespaced so a future
# consumer reading the preprocessed GeoJSON with a generic GIS tool
# doesn't collide with common property names.
LAYER_KEY = "bv:layer"

# Standard OpenCPN symbol names the router emits on navaids. S-57
# categories and OSM seamark:type values map into this set during
# preprocessing; the GPX emitter passes them through verbatim.
#
# See https://opencpn.org for the canonical symbol palette. We keep
# just the ones we actually emit — unknowns become "Waypoint".
NAVAID_SYMS: frozenset[str] = frozenset(
    {
        "Buoy, Red",
        "Buoy, Green",
        "Buoy, White",
        "Buoy, Yellow",
        "Beacon, Red",
        "Beacon, Green",
        "Beacon, White",
        "Light, Red",
        "Light, Green",
        "Light, White",
        "Light",
        "Navaid, Red",
        "Navaid, Green",
        "Navaid, White",
        "Waypoint",
    }
)


__all__ = ["LAYERS", "LAYER_KEY", "NAVAID_SYMS", "Layer"]
