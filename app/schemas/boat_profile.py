"""BoatProfile pydantic schemas for the /boat_profiles API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class BoatProfileIn(BaseModel):
    """PUT /boat_profiles/{name} body."""

    model_config = ConfigDict(extra="forbid")

    polar_path: str
    draft_m: Annotated[float, Field(gt=0, le=10)]
    beam_m: Annotated[float, Field(gt=0, le=20)]
    max_wind_kts: Annotated[float, Field(gt=0, le=80)] = 30.0
    max_seas_m: Annotated[float, Field(gt=0, le=10)] = 2.5
    min_depth_m: Annotated[float, Field(ge=0, le=5)] = 0.5
    night_sailing_ok: bool = True
    motor_available: bool = False
    motor_min_wind_kts: Annotated[float, Field(ge=0, le=30)] | None = None


class BoatProfileOut(BoatProfileIn):
    name: str
    created_at: datetime
    updated_at: datetime


class BoatProfileSummary(BaseModel):
    name: str
    draft_m: float
    beam_m: float
    polar_path: str
