"""`python -m app.charts fetch ...` implementation.

Exit codes:

- 0 — coverage complete, bbox fully seeded.
- 2 — `CHARTS_NOT_AVAILABLE`: ENC + OSM combined can't cover the bbox,
  or GEBCO is unconfigured / missing.
- 3 — `CHARTS_FETCH_FAILED`: upstream network error.
- 64 — usage error (bad --bbox, unknown --region, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from app.config import get_settings
from app.logging import get_logger
from app.services.charts import (
    ChartsCoverageError,
    ChartsFetchError,
    ChartStore,
)

log = get_logger(__name__)

Bbox = tuple[float, float, float, float]


# Named regions are intentionally small — add as we support them.
# Each bbox is (lat_min, lon_min, lat_max, lon_max).
NAMED_REGIONS: dict[str, Bbox] = {
    "chesapeake": (36.5, -77.5, 39.5, -75.5),
    "long_island_sound": (40.5, -74.0, 41.5, -71.5),
    "san_francisco_bay": (37.4, -123.0, 38.2, -121.8),
    "puget_sound": (47.0, -123.5, 49.0, -122.0),
    "maine_coast": (43.0, -71.0, 45.0, -66.5),
}


@dataclass
class CliArgs:
    bbox: Bbox
    label: str  # "bbox=..." or "region=chesapeake"


def _parse_bbox(raw: str) -> Bbox:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "--bbox must be lat_min,lon_min,lat_max,lon_max"
        )
    try:
        lat_min, lon_min, lat_max, lon_max = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--bbox: {exc}") from exc
    if not (-90 <= lat_min <= lat_max <= 90):
        raise argparse.ArgumentTypeError("--bbox: latitudes out of range")
    if not (-180 <= lon_min <= lon_max <= 180):
        raise argparse.ArgumentTypeError("--bbox: longitudes out of range")
    return (lat_min, lon_min, lat_max, lon_max)


def _resolve(args: argparse.Namespace) -> CliArgs:
    if args.region is not None:
        key = args.region.lower()
        if key not in NAMED_REGIONS:
            raise SystemExit(
                f"unknown --region: {args.region!r}. "
                f"Known: {', '.join(sorted(NAMED_REGIONS))}"
            )
        return CliArgs(bbox=NAMED_REGIONS[key], label=f"region={args.region}")
    return CliArgs(bbox=args.bbox, label=f"bbox={','.join(f'{v:.4f}' for v in args.bbox)}")


async def _run_fetch(resolved: CliArgs) -> int:
    settings = get_settings()
    if settings.chart_store_mode != "real":
        print(
            f"BV_CHART_STORE_MODE={settings.chart_store_mode!r}; CLI only fetches "
            "with the real ChartStore. Set BV_CHART_STORE_MODE=real and retry.",
            file=sys.stderr,
        )
        return 64

    gebco_path = settings.effective_gebco_path()
    store = ChartStore(base_dir=settings.charts_dir, gebco_path=gebco_path)
    print(
        f"charts.fetch starting {resolved.label} "
        f"charts_dir={settings.charts_dir} gebco={gebco_path}",
        file=sys.stderr,
    )
    try:
        await store.ensure_coverage(resolved.bbox)
    except ChartsCoverageError as exc:
        print(f"CHARTS_NOT_AVAILABLE: {exc}", file=sys.stderr)
        return 2
    except ChartsFetchError as exc:
        print(f"CHARTS_FETCH_FAILED: {exc}", file=sys.stderr)
        return 3

    coverage = await store.coverage(resolved.bbox)
    print(
        f"charts.fetch done enc_cells={coverage.enc_cells} "
        f"osm_extracts={coverage.osm_extracts} gebco_tile={coverage.gebco_tile}",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.charts",
        description="better-voyage chart ingest CLI (plan/15).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser(
        "fetch",
        help="download + preprocess chart data for a bbox or named region",
    )
    sel = f.add_mutually_exclusive_group(required=True)
    sel.add_argument(
        "--bbox",
        type=_parse_bbox,
        help="lat_min,lon_min,lat_max,lon_max (WGS84)",
    )
    sel.add_argument(
        "--region",
        choices=sorted(NAMED_REGIONS),
        help="named region (see NAMED_REGIONS for the list)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd != "fetch":  # argparse rejects anything else already
        parser.error(f"unknown command: {args.cmd}")
    resolved = _resolve(args)
    return asyncio.run(_run_fetch(resolved))


__all__ = ["NAMED_REGIONS", "build_parser", "main"]
