"""Voyage table — the async-job state machine.

Per plan/11 §voyages: a voyage IS a job. One row per submission (with
single-voyage retention replacing older rows on submit). `status` is
the state-machine field; `progress_json` carries the current stage's
progress blob; `gpx_blob` is null until `finalizing` completes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Voyage(Base):
    __tablename__ = "voyages"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # vy_<ulid>
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(String, index=True)
    progress_json: Mapped[str] = mapped_column(Text, default="{}")

    request_json: Mapped[str] = mapped_column(Text)
    inputs_hash: Mapped[str] = mapped_column(String, index=True)

    gpx_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    coverage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_trace_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_stage: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_voyages_status_created_at", "status", "created_at"),
    )
