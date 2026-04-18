"""Chart ingest CLI (plan/15 §Chart fetching — the synchronous path).

`python -m app.charts fetch --bbox lat_min,lon_min,lat_max,lon_max` or
`python -m app.charts fetch --region <name>` pre-seeds the chart cache
for a region. Same machinery as the voyage-job `charts_fetching` stage
(it calls `ChartStore.ensure_coverage` directly), just driven from the
shell rather than from an async voyage job. Recommended before leaving
the dock for areas with no connectivity.
"""

from app.charts.cli import main

__all__ = ["main"]
