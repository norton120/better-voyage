"""Inbound request schemas for voyage planning.

Mirrors plan/10 §POST /voyages. `canonicalize()` and `compute_inputs_hash()`
implement the canonicalization rule (round times to the minute, strip
`max_candidates`, alphabetize fields) used for the idempotency key.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_WINDOW_DAYS = 14


class Coord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]
    name: str | None = None


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_at: datetime
    end_at: datetime
    tz: str = "UTC"
    earliest_departure_local_time: time | None = None
    latest_departure_local_time: time | None = None

    @model_validator(mode="after")
    def _check_window(self) -> TimeWindow:
        if self.start_at >= self.end_at:
            raise ValueError("INVALID_WINDOW: start_at must be before end_at")
        span_days = (self.end_at - self.start_at).total_seconds() / 86400.0
        if span_days > MAX_WINDOW_DAYS:
            raise ValueError(
                f"INVALID_WINDOW: window is {span_days:.1f} days; "
                f"max is {MAX_WINDOW_DAYS}"
            )
        return self


Objective = Literal["fastest", "comfortable", "short_tacks"]


class VoyageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: Coord
    destination: Coord
    window: TimeWindow
    boat_profile_name: str
    objective: Objective = "fastest"
    max_candidates: Annotated[int, Field(ge=1, le=20)] = 5

    @model_validator(mode="after")
    def _check_endpoints(self) -> VoyageRequest:
        if (
            abs(self.origin.lat - self.destination.lat) < 1e-9
            and abs(self.origin.lon - self.destination.lon) < 1e-9
        ):
            raise ValueError("INVALID_WINDOW: origin and destination are identical")
        return self


def _round_to_minute(dt: datetime) -> datetime:
    """Truncate to whole minutes, anchored at UTC."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.replace(second=0, microsecond=0)


def canonicalize(req: VoyageRequest) -> dict:
    """Stable representation for hashing.

    - Times rounded to the minute, serialized as ISO-8601 Zulu.
    - `max_candidates` stripped (tuning knob, not a semantic input).
    - Keys JSON-sorted at dump time.
    """
    return {
        "boat_profile_name": req.boat_profile_name,
        "destination": {
            "lat": round(req.destination.lat, 6),
            "lon": round(req.destination.lon, 6),
            "name": req.destination.name,
        },
        "objective": req.objective,
        "origin": {
            "lat": round(req.origin.lat, 6),
            "lon": round(req.origin.lon, 6),
            "name": req.origin.name,
        },
        "window": {
            "earliest_departure_local_time": (
                req.window.earliest_departure_local_time.isoformat()
                if req.window.earliest_departure_local_time
                else None
            ),
            "end_at": _round_to_minute(req.window.end_at).isoformat(),
            "latest_departure_local_time": (
                req.window.latest_departure_local_time.isoformat()
                if req.window.latest_departure_local_time
                else None
            ),
            "start_at": _round_to_minute(req.window.start_at).isoformat(),
            "tz": req.window.tz,
        },
    }


def compute_inputs_hash(req: VoyageRequest) -> str:
    """sha256 of the canonicalized request, prefixed for readability."""
    payload = json.dumps(canonicalize(req), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
