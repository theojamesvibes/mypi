from __future__ import annotations

import asyncio

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.models.user import User
from app.services import sync_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])


class SyncRequest(BaseModel):
    import_config: bool = True
    import_gravity: bool = True
    import_dhcp_leases: bool = False
    run_gravity: bool = True


class ScheduleRequest(BaseModel):
    interval_minutes: int = 0   # 0 = disabled
    auto_gravity: bool = False  # auto-sync when master blocklist count changes
    import_config: bool = True
    import_gravity: bool = True
    import_dhcp_leases: bool = False
    run_gravity: bool = True


class InstanceResult(BaseModel):
    name: str
    status: str
    error: str | None = None


class SyncStatusResponse(BaseModel):
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    master: str | None = None
    results: list[InstanceResult] = []
    error: str | None = None


def _state_to_response(state: sync_service.SyncState) -> SyncStatusResponse:
    return SyncStatusResponse(
        status=state.status,
        started_at=state.started_at.isoformat() if state.started_at else None,
        completed_at=state.completed_at.isoformat() if state.completed_at else None,
        master=state.master,
        results=[InstanceResult(name=r.name, status=r.status, error=r.error) for r in state.results],
        error=state.error,
    )


@router.get("/status", response_model=SyncStatusResponse)
async def sync_status(_: User = Depends(get_current_user)) -> SyncStatusResponse:
    return _state_to_response(sync_service.get_state())


@router.get("/schedule")
async def get_schedule(_: User = Depends(get_current_user)) -> dict:
    return sync_service.get_schedule()


@router.put("/schedule")
async def set_schedule(req: ScheduleRequest, _: User = Depends(get_current_user)) -> dict:
    try:
        await sync_service.set_schedule(
            interval_minutes=req.interval_minutes,
            auto_gravity=req.auto_gravity,
            import_config=req.import_config,
            import_gravity=req.import_gravity,
            import_dhcp_leases=req.import_dhcp_leases,
            run_gravity=req.run_gravity,
        )
    except Exception as exc:
        logger.exception("Failed to persist sync schedule: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save sync schedule.")
    return sync_service.get_schedule()


@router.post("", response_model=SyncStatusResponse)
async def trigger_sync(
    req: SyncRequest,
    background_tasks: BackgroundTasks,
    _: User = Depends(get_current_user),
) -> SyncStatusResponse:
    if sync_service.get_state().status == "running":
        raise HTTPException(status_code=409, detail="A sync is already in progress.")

    background_tasks.add_task(
        sync_service.run_sync,
        import_config=req.import_config,
        import_gravity=req.import_gravity,
        import_dhcp_leases=req.import_dhcp_leases,
        run_gravity=req.run_gravity,
    )

    return SyncStatusResponse(status="running")
