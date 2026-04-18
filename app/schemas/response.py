"""Outbound response schemas for the voyages API.

Keep these narrow — they're the shape clients poll. The full voyage
document (plan/10 §voyage shape) is GPX-native and assembled by the
planner into `voyages.gpx_blob` at `finalizing`; we surface a parsed
JSON mirror via `voyage` when `status="done"`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

VoyageStatus = Literal[
    "queued",
    "charts_fetching",
    "charts_preprocessing",
    "forecast_prefetching",
    "routing",
    "scoring",
    "finalizing",
    "done",
    "failed",
    "cancelling",
    "cancelled",
]


class Progress(BaseModel):
    stage: str
    pct: float = 0.0
    detail: str | None = None
    eta_s: float | None = None


class VoyageError(BaseModel):
    code: str
    detail: str | None = None
    stage: str | None = None


class Links(BaseModel):
    self: str
    events: str
    gpx: str
    trace: str
    cancel: str


class VoyageState(BaseModel):
    """GET /voyages/{id} payload."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: VoyageStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    progress: Progress
    voyage: dict[str, Any] | None = None
    error: VoyageError | None = None
    links: Links


class AcceptedResponse(BaseModel):
    """POST /voyages 202 payload."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: VoyageStatus
    created_at: datetime
    progress: Progress
    links: Links


class CancelResponse(BaseModel):
    id: str
    status: VoyageStatus
