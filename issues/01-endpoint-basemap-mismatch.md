# Endpoint / basemap data mismatch

## Problem

A user clicks two points on the map that are **clearly in water** at the displayed zoom, submits, and the voyage fails with `ROUTE_BLOCKED` / `ROUTE_NO_COVERAGE`. Inspecting the data, one or both endpoints is classified as *on land* by the router's chart data — `distance_to_land_nm = 0.0`, inside a land polygon.

Observed concretely on `(38.33, -76.45)` (looks like open Chesapeake at zoom 7; ENC/OSM preprocessed land layer says it's inside Lexington Park, MD). The Leaflet basemap the user sees (`tile.openstreetmap.org`) and the `natural=coastline` + ENC `LNDARE` polygons we've indexed disagree on where the water/land boundary is.

Separately, there's no submit-time validation for endpoints on land, so the pipeline runs through `charts_fetching` → `forecast_prefetching` → dozens of candidate routes before failing. First feedback to the user is minutes of spinner then an opaque `ROUTE_BLOCKED`.

## How to validate a solution

1. Click a pick that visually sits on land at the current zoom — submit is refused immediately, with a message that names the endpoint (origin vs. destination) and points at something the user can do (drag the marker, zoom in).
2. Click two picks that visually sit in water at the current zoom — submit proceeds through the pipeline without the "on-land" class of failure.
3. No silent divergence: the set of positions the UI accepts is exactly the set the router considers navigable. Whichever data source is authoritative, both the UI and the router must agree.

Non-goal: fixing the coastline itself or replacing GEBCO's 15-arcsec grid. The goal is consistency between what the user sees and what the router accepts, with an early/clear error when they diverge.

## Possible solutions

- **Client-side pre-validation against the server's land index.** New endpoint `GET /charts/point?lat=..&lon=..` returns `{in_water: bool, depth_m, distance_to_land_nm}` using the same `ChartStore` the router queries. UI calls it on each map click, blocks submit (or offers to snap) when `in_water` is false. Keeps the authoritative source as the ENC/OSM data the router actually uses; makes the UI honest about what's navigable.

- **Snap to nearest water on submit.** If the picked endpoint is in a land polygon, automatically move it to the nearest point outside land polygons (via STRtree nearest + buffer), preserving the user's pick as a named `<wpt>` in the GPX. Convenient but hides divergence and can silently move a marina pick kilometres away.

- **Swap the basemap for one aligned with our land data.** Serve OpenSeaMap (or a custom tile layer built from the same ENC/OSM extract) so the visible coastline is the one the router uses. Fixes the root cause but much heavier to build/ops, and doesn't help picks that fall near GEBCO's coarse grid cells.

## Useful context

- Router queries already exposed: `ChartStore.distance_to_land_nm(lat, lon)`, `chart_depth(lat, lon)`, `is_restricted((lat, lon))` — a cheap `/charts/point` endpoint can compose them.
- Current UI flow: `app/ui/router.py:submit_voyage` accepts form-encoded lat/lon with no water check; `app/ui/static/ui.js:map.on('click')` is where picks are registered.
- Leaflet basemap is hard-coded to `https://tile.openstreetmap.org/{z}/{x}/{y}.png` in `app/ui/static/ui.js`.
- The `ChartStore` is a process-wide singleton; the first query after startup blocks on `ensure_coverage` for the relevant bbox. Any "validate on click" path must handle "coverage not yet loaded" gracefully.
