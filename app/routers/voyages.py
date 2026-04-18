"""Voyages HTTP surface.

Implements the idempotency + single-voyage-retention flow from plan/10
and plan/11: at most one voyage row exists at a time; every
`POST /voyages` either reuses, replaces, or (on conflicting live work)
rejects with 409.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import Response as FastAPIResponse

from app.clients.cache import as_aware_utc, utc_now
from app.config import get_settings
from app.logging import get_logger
from app.models.voyage import Voyage
from app.schemas.request import VoyageRequest, compute_inputs_hash
from app.schemas.response import (
    AcceptedResponse,
    CancelResponse,
    Links,
    Progress,
    VoyageError,
    VoyageState,
)
from app.services import boat_profiles
from app.services.jobs import (
    LIVE_STAGES,
    JobRegistry,
    delete_voyage,
    find_existing,
    insert_voyage,
)

log = get_logger(__name__)
router = APIRouter(prefix="/voyages", tags=["voyages"])


def _links(voyage_id: str) -> Links:
    base = f"/voyages/{voyage_id}"
    return Links(
        self=base, events=f"{base}/events", gpx=f"{base}/gpx",
        trace=f"{base}/trace", cancel=f"{base}/cancel",
    )


def _progress(row: Voyage) -> Progress:
    try:
        payload: dict[str, Any] = json.loads(row.progress_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    return Progress(
        stage=payload.get("stage", row.status),
        pct=float(payload.get("pct") or 0.0),
        detail=payload.get("detail"),
        eta_s=payload.get("eta_s"),
    )


def _voyage_doc(row: Voyage) -> dict[str, Any] | None:
    """Parsed JSON mirror of the voyage body, surfaced when `done`.

    Also surfaced when the voyage failed offline with cached context
    (OFFLINE_NO_ROUTE) so the client can show the stale-data hint.
    """
    coverage: dict[str, Any] | None = None
    if row.coverage_json:
        try:
            coverage = json.loads(row.coverage_json)
        except json.JSONDecodeError:
            coverage = None
    if row.status == "done":
        return {
            "metadata": {"name": "voyage"},
            "gpx_available": row.gpx_blob is not None,
            "coverage": coverage,
        }
    if row.error_code == "OFFLINE_NO_ROUTE" and coverage is not None:
        return {
            "metadata": {"name": "voyage"},
            "gpx_available": False,
            "coverage": coverage,
        }
    return None


def _state(row: Voyage) -> VoyageState:
    return VoyageState(
        id=row.id,
        status=row.status,  # type: ignore[arg-type]
        created_at=as_aware_utc(row.created_at),
        started_at=as_aware_utc(row.started_at) if row.started_at else None,
        completed_at=as_aware_utc(row.completed_at) if row.completed_at else None,
        progress=_progress(row),
        voyage=_voyage_doc(row),
        error=(
            VoyageError(
                code=row.error_code,
                detail=row.error_detail,
                stage=row.error_stage,
            )
            if row.error_code
            else None
        ),
        links=_links(row.id),
    )


def _accepted(row: Voyage) -> AcceptedResponse:
    return AcceptedResponse(
        id=row.id,
        status=row.status,  # type: ignore[arg-type]
        created_at=as_aware_utc(row.created_at),
        progress=_progress(row),
        links=_links(row.id),
    )


def _registry(request: Request) -> JobRegistry:
    reg = getattr(request.app.state, "registry", None)
    if reg is None:
        raise HTTPException(500, detail="JobRegistry not initialized")
    return reg


def _is_done_within_ttl(row: Voyage) -> bool:
    if row.status != "done" or row.completed_at is None:
        return False
    ttl = timedelta(seconds=get_settings().forecast_cache_ttl_s)
    return (utc_now() - as_aware_utc(row.completed_at)) < ttl


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a voyage plan",
    response_model=AcceptedResponse,
)
async def post_voyage(
    req: VoyageRequest,
    request: Request,
    response: Response,
    force: bool = Query(False, description="Replace any live voyage"),
) -> AcceptedResponse:
    registry = _registry(request)
    if await boat_profiles.get(req.boat_profile_name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "BOAT_PROFILE_NOT_FOUND", "name": req.boat_profile_name},
        )
    inputs_hash = compute_inputs_hash(req)
    existing = await find_existing()

    if force and existing is not None and existing.status in LIVE_STAGES:
        await registry.cancel(existing.id)
        await delete_voyage(existing.id)
        existing = None
    elif force and existing is not None:
        await delete_voyage(existing.id)
        existing = None

    if existing is None:
        vid = await insert_voyage(req)
        await registry.submit(vid)
        row = await _load(vid)
        response.status_code = status.HTTP_202_ACCEPTED
        return _accepted(row)

    same_hash = existing.inputs_hash == inputs_hash
    live = existing.status in LIVE_STAGES or existing.status == "queued"

    if same_hash and live:
        # Dedupe — return the live voyage.
        response.status_code = status.HTTP_202_ACCEPTED
        return _accepted(existing)

    if same_hash and _is_done_within_ttl(existing):
        # 303 redirect to the still-valid result.
        response.headers["Location"] = f"/voyages/{existing.id}"
        response.status_code = status.HTTP_303_SEE_OTHER
        return _accepted(existing)

    if not same_hash and live:
        raise HTTPException(
            status_code=409,
            detail={"code": "VOYAGE_IN_PROGRESS", "id": existing.id},
        )

    # Replace: existing is terminal (done past TTL, failed, cancelled)
    # or hash differs and not live.
    await delete_voyage(existing.id)
    vid = await insert_voyage(req)
    await registry.submit(vid)
    row = await _load(vid)
    response.status_code = status.HTTP_202_ACCEPTED
    return _accepted(row)


@router.get("/{voyage_id}", summary="Voyage state")
async def get_voyage(voyage_id: str, response: Response) -> VoyageState:
    row = await _load(voyage_id)
    if row.error_code == "OFFLINE_NO_ROUTE":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return _state(row)


@router.post("/{voyage_id}/cancel", summary="Cancel a running voyage")
async def cancel_voyage(voyage_id: str, request: Request) -> CancelResponse:
    registry = _registry(request)
    row = await _load(voyage_id)
    await registry.cancel(row.id)
    row = await _load(voyage_id)
    return CancelResponse(id=row.id, status=row.status)  # type: ignore[arg-type]


@router.get(
    "/{voyage_id}/gpx",
    summary="Download the voyage GPX file",
    response_class=FastAPIResponse,
)
async def get_gpx(voyage_id: str) -> FastAPIResponse:
    row = await _load(voyage_id)
    if row.status != "done" or row.gpx_blob is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "VOYAGE_NOT_READY", "status": row.status},
        )
    filename = f"voyage-{row.id}.gpx"
    return FastAPIResponse(
        content=row.gpx_blob,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{voyage_id}/trace", summary="Progressively populated PlanTrace")
async def get_trace(voyage_id: str) -> dict[str, Any]:
    row = await _load(voyage_id)
    if row.plan_trace_json:
        return json.loads(row.plan_trace_json)
    return {"voyage_id": row.id, "status": row.status, "trace": []}


# --- helpers ----------------------------------------------------------------


async def _load(voyage_id: str) -> Voyage:
    from app.db import session_scope

    async with session_scope() as session:
        row = await session.get(Voyage, voyage_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "VOYAGE_NOT_FOUND", "id": voyage_id},
        )
    return row
