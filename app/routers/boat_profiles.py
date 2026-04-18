"""Boat profile CRUD."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.clients.cache import as_aware_utc
from app.models.boat_profile import BoatProfile
from app.schemas.boat_profile import BoatProfileIn, BoatProfileOut, BoatProfileSummary
from app.services import boat_profiles

router = APIRouter(prefix="/boat_profiles", tags=["boat_profiles"])


def _to_out(row: BoatProfile) -> BoatProfileOut:
    return BoatProfileOut(
        name=row.name,
        polar_path=row.polar_path,
        draft_m=row.draft_m,
        beam_m=row.beam_m,
        max_wind_kts=row.max_wind_kts,
        max_seas_m=row.max_seas_m,
        min_depth_m=row.min_depth_m,
        night_sailing_ok=row.night_sailing_ok,
        motor_available=row.motor_available,
        motor_min_wind_kts=row.motor_min_wind_kts,
        created_at=as_aware_utc(row.created_at),
        updated_at=as_aware_utc(row.updated_at),
    )


@router.get("", response_model=list[BoatProfileSummary], summary="List boat profiles")
async def list_profiles() -> list[BoatProfileSummary]:
    rows = await boat_profiles.list_all()
    return [
        BoatProfileSummary(
            name=r.name, draft_m=r.draft_m, beam_m=r.beam_m, polar_path=r.polar_path
        )
        for r in rows
    ]


@router.get("/{name}", response_model=BoatProfileOut, summary="Get a boat profile")
async def get_profile(name: str) -> BoatProfileOut:
    row = await boat_profiles.get(name)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "BOAT_PROFILE_NOT_FOUND", "name": name},
        )
    return _to_out(row)


@router.put(
    "/{name}",
    response_model=BoatProfileOut,
    status_code=status.HTTP_200_OK,
    summary="Create or replace a boat profile",
)
async def put_profile(name: str, body: BoatProfileIn) -> BoatProfileOut:
    row = await boat_profiles.upsert(name, body)
    return _to_out(row)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a boat profile")
async def delete_profile(name: str) -> None:
    ok = await boat_profiles.delete(name)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail={"code": "BOAT_PROFILE_NOT_FOUND", "name": name},
        )
    return None
