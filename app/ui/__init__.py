"""Server-rendered HTMX + Leaflet UI (plan/13 stretch §web UI).

A thin skipper-facing face over the JSON voyages API: click map to pick
origin / destination, fill the window + boat form, submit, watch stage
progress, pick a candidate, download GPX. HTMX drives the polling and
partial swaps; Leaflet handles map interaction. No frontend build step.
"""

from app.ui.router import router

__all__ = ["router"]
