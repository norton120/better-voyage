"""Segment-test LRU around `crosses_land` / `crosses_obstacle`.

plan/17 step 1: cached answers must be correct and must not outlive
the spatial trees they were computed against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import Polygon

from app.services.charts import (
    ChartStore,
    _LoadedLayers,
    _LoadedSource,
)


def _store_with_land_square(tmp_path: Path) -> ChartStore:
    """A ChartStore with a single 1°×1° land polygon around (0,0)."""
    store = ChartStore(base_dir=tmp_path)
    land = Polygon([(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)])
    src = _LoadedSource(
        source_id="test",
        kind="osm",
        bbox=(-1.0, -1.0, 1.0, 1.0),
        fetched_at=datetime.now(UTC),
        layers=_LoadedLayers(land=[land]),
    )
    store._sources[src.source_id] = src
    store._rebuild_indices()
    return store


def test_crosses_land_cache_hits_on_repeat(tmp_path: Path) -> None:
    store = _store_with_land_square(tmp_path)
    a = (0.0, -1.0)
    b = (0.0, 1.0)
    assert store.crosses_land(a, b) is True
    # Second call must hit the cache. We don't have direct hit-count
    # access to the OTel counter, so we inspect the OrderedDict: one
    # entry keyed on the quantized segment.
    assert len(store._segment_cache) == 1
    assert store.crosses_land(a, b) is True
    assert len(store._segment_cache) == 1


def test_crosses_land_cache_preserves_result(tmp_path: Path) -> None:
    store = _store_with_land_square(tmp_path)
    on_land = store.crosses_land((0.0, -1.0), (0.0, 1.0))
    off_land = store.crosses_land((2.0, -1.0), (2.0, 1.0))
    assert on_land is True
    assert off_land is False
    # Exercise again — results must match across cache hits.
    assert store.crosses_land((0.0, -1.0), (0.0, 1.0)) is True
    assert store.crosses_land((2.0, -1.0), (2.0, 1.0)) is False


def test_land_and_obstacle_do_not_collide_in_cache(tmp_path: Path) -> None:
    store = _store_with_land_square(tmp_path)
    # Same segment, different layers → different cache keys.
    store.crosses_land((0.0, -1.0), (0.0, 1.0))
    store.crosses_obstacle((0.0, -1.0), (0.0, 1.0))
    assert len(store._segment_cache) == 2


def test_rebuild_indices_clears_cache(tmp_path: Path) -> None:
    store = _store_with_land_square(tmp_path)
    store.crosses_land((0.0, -1.0), (0.0, 1.0))
    assert store._segment_cache
    store._rebuild_indices()
    assert not store._segment_cache


def test_safety_margin_rejects_near_miss(tmp_path: Path) -> None:
    """A segment that passes ~0.3 nm clear of land is clean at margin=0
    and rejected at margin=0.5 nm."""
    store = _store_with_land_square(tmp_path)
    # Land box spans lat ∈ [-0.5, 0.5], lon ∈ [-0.5, 0.5]. Run a segment
    # at lat = 0.505 (≈ 0.005 deg ≈ 0.3 nm north of the land edge).
    a = (0.505, -1.0)
    b = (0.505, 1.0)
    assert store.crosses_land(a, b, margin_nm=0.0) is False
    assert store.crosses_land(a, b, margin_nm=0.5) is True


def test_margin_keys_cache_independently(tmp_path: Path) -> None:
    """The same segment with different margins must not alias in cache."""
    store = _store_with_land_square(tmp_path)
    a, b = (0.505, -1.0), (0.505, 1.0)
    store.crosses_land(a, b, margin_nm=0.0)
    store.crosses_land(a, b, margin_nm=0.5)
    assert len(store._segment_cache) == 2
