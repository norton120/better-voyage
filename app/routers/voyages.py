from fastapi import APIRouter, status

router = APIRouter(prefix="/voyages", tags=["voyages"])


@router.post(
    "",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    summary="Plan a voyage (stub)",
)
async def plan_voyage() -> dict[str, str]:
    return {"detail": "not implemented"}
