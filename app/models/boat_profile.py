"""BoatProfile table.

Per plan/01 §BoatProfile + plan/10 §Boat profiles. Profiles are
referenced from `VoyageRequest.boat_profile_name` and copied into the
voyage's `bv:request` extension so the passage is reproducible from
the saved GPX alone.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BoatProfile(Base):
    __tablename__ = "boat_profiles"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    polar_path: Mapped[str] = mapped_column(Text)
    draft_m: Mapped[float] = mapped_column(Float)
    beam_m: Mapped[float] = mapped_column(Float)
    max_wind_kts: Mapped[float] = mapped_column(Float)
    max_seas_m: Mapped[float] = mapped_column(Float)
    min_depth_m: Mapped[float] = mapped_column(Float, default=0.5)
    night_sailing_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    motor_available: Mapped[bool] = mapped_column(Boolean, default=False)
    motor_min_wind_kts: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
