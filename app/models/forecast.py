"""Cache tables for upstream weather, tide, and station payloads.

Schema per plan/11-storage-caching.md. These tables back the generic
TTL cache wrapper in `app.clients.cache`; the client layer is the only
caller.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ForecastCache(Base):
    __tablename__ = "forecast_cache"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    params_json: Mapped[str] = mapped_column(Text)
    body_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TideCache(Base):
    __tablename__ = "tide_cache"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    station_id: Mapped[str] = mapped_column(String, index=True)
    body_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class StationsCache(Base):
    __tablename__ = "stations_cache"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    name: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
