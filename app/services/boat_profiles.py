"""BoatProfile data-access helpers (DB-side only, no HTTP)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.clients.cache import utc_now
from app.db import session_scope
from app.models.boat_profile import BoatProfile
from app.schemas.boat_profile import BoatProfileIn

DEFAULT_PROFILE_NAME = "default"


async def get(name: str) -> BoatProfile | None:
    async with session_scope() as session:
        return await session.get(BoatProfile, name)


async def list_all() -> list[BoatProfile]:
    async with session_scope() as session:
        result = await session.execute(select(BoatProfile).order_by(BoatProfile.name))
        return list(result.scalars().all())


async def upsert(name: str, data: BoatProfileIn) -> BoatProfile:
    now = utc_now()
    async with session_scope() as session:
        existing = await session.get(BoatProfile, name)
        created_at = existing.created_at if existing is not None else now
        stmt = sqlite_insert(BoatProfile).values(
            name=name,
            polar_path=data.polar_path,
            draft_m=data.draft_m,
            beam_m=data.beam_m,
            max_wind_kts=data.max_wind_kts,
            max_seas_m=data.max_seas_m,
            min_depth_m=data.min_depth_m,
            night_sailing_ok=data.night_sailing_ok,
            motor_available=data.motor_available,
            motor_min_wind_kts=data.motor_min_wind_kts,
            created_at=created_at,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[BoatProfile.name],
            set_={
                "polar_path": stmt.excluded.polar_path,
                "draft_m": stmt.excluded.draft_m,
                "beam_m": stmt.excluded.beam_m,
                "max_wind_kts": stmt.excluded.max_wind_kts,
                "max_seas_m": stmt.excluded.max_seas_m,
                "min_depth_m": stmt.excluded.min_depth_m,
                "night_sailing_ok": stmt.excluded.night_sailing_ok,
                "motor_available": stmt.excluded.motor_available,
                "motor_min_wind_kts": stmt.excluded.motor_min_wind_kts,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await session.execute(stmt)
        row = await session.get(BoatProfile, name)
        assert row is not None
        return row


async def delete(name: str) -> bool:
    async with session_scope() as session:
        row = await session.get(BoatProfile, name)
        if row is None:
            return False
        await session.delete(row)
        return True


async def ensure_default_seeded() -> None:
    """Upsert the 'default' profile pointing at the shipped polar.

    Called from the FastAPI lifespan. Safe to call repeatedly — the
    upsert preserves `created_at` on existing rows.
    """
    from app.services.polars import DEFAULT_POLAR_PATH

    await upsert(
        DEFAULT_PROFILE_NAME,
        BoatProfileIn(
            polar_path=str(DEFAULT_POLAR_PATH),
            draft_m=1.8,
            beam_m=3.8,
            max_wind_kts=30.0,
            max_seas_m=2.5,
            min_depth_m=0.5,
            night_sailing_ok=True,
            motor_available=False,
            motor_min_wind_kts=None,
        ),
    )
