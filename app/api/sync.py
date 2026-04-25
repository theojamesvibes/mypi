import asyncio

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api._site_dep import resolve_site
from app.auth import get_current_user, require_mutation
from app.limiter import limiter
from app.models.site import Site
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
    vip_role: str | None = None


class SyncStatusResponse(BaseModel):
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    master: str | None = None
    master_vip_role: str | None = None
    results: list[InstanceResult] = []
    error: str | None = None


def _state_to_response(state: sync_service.SyncState) -> SyncStatusResponse:
    return SyncStatusResponse(
        status=state.status,
        started_at=state.started_at.isoformat() if state.started_at else None,
        completed_at=state.completed_at.isoformat() if state.completed_at else None,
        master=state.master,
        master_vip_role=state.master_vip_role,
        results=[
            InstanceResult(name=r.name, status=r.status, error=r.error, vip_role=r.vip_role)
            for r in state.results
        ],
        error=state.error,
    )


@router.get("/status", response_model=SyncStatusResponse)
async def sync_status(_: User = Depends(get_current_user)) -> SyncStatusResponse:
    return _state_to_response(await sync_service.get_state())


@router.get("/schedule")
async def get_schedule(_: User = Depends(get_current_user)) -> dict:
    return await sync_service.get_schedule()


@router.put("/schedule")
async def set_schedule(req: ScheduleRequest, user: User = Depends(require_mutation)) -> dict:
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
    logger.info(
        "user=%s set sync schedule: interval=%dmin auto_gravity=%s",
        user.username, req.interval_minutes, req.auto_gravity,
    )
    return await sync_service.get_schedule()


@router.post("", response_model=SyncStatusResponse)
@limiter.limit("10/minute")
async def trigger_sync(
    request: Request,
    req: SyncRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_mutation),
) -> SyncStatusResponse:
    current_state = await sync_service.get_state()
    if current_state.status == "running":
        raise HTTPException(status_code=409, detail="A sync is already in progress.")

    logger.info("user=%s triggered manual sync", user.username)
    background_tasks.add_task(
        sync_service.run_sync,
        import_config=req.import_config,
        import_gravity=req.import_gravity,
        import_dhcp_leases=req.import_dhcp_leases,
        run_gravity=req.run_gravity,
    )

    return SyncStatusResponse(status="running")


# ── Per-site variants ────────────────────────────────────────────────────────
# The legacy routes above resolve Main internally (sync_service defaults to
# Main when site_id=None). These routes accept an explicit {slug} so
# multi-site deployments and the web UI / iOS app can target a specific site.

site_router = APIRouter(prefix="/api/sites/{slug}/sync", tags=["sync (per-site)"])


@site_router.get("/status", response_model=SyncStatusResponse)
async def sync_status_for_site(
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
) -> SyncStatusResponse:
    return _state_to_response(await sync_service.get_state(site_id=site.id))


@site_router.get("/schedule")
async def get_schedule_for_site(
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
) -> dict:
    return await sync_service.get_schedule(site_id=site.id)


@site_router.put("/schedule")
async def set_schedule_for_site(
    req: ScheduleRequest,
    site: Site = Depends(resolve_site),
    user: User = Depends(require_mutation),
) -> dict:
    try:
        await sync_service.set_schedule(
            interval_minutes=req.interval_minutes,
            auto_gravity=req.auto_gravity,
            import_config=req.import_config,
            import_gravity=req.import_gravity,
            import_dhcp_leases=req.import_dhcp_leases,
            run_gravity=req.run_gravity,
            site_id=site.id,
        )
    except Exception as exc:
        logger.exception("Failed to persist sync schedule for site %s: %s", site.slug, exc)
        raise HTTPException(status_code=500, detail="Failed to save sync schedule.")
    logger.info(
        "user=%s set sync schedule for site '%s': interval=%dmin auto_gravity=%s",
        user.username, site.name, req.interval_minutes, req.auto_gravity,
    )
    return await sync_service.get_schedule(site_id=site.id)


@site_router.post("", response_model=SyncStatusResponse)
@limiter.limit("10/minute")
async def trigger_sync_for_site(
    request: Request,
    req: SyncRequest,
    background_tasks: BackgroundTasks,
    site: Site = Depends(resolve_site),
    user: User = Depends(require_mutation),
) -> SyncStatusResponse:
    current_state = await sync_service.get_state(site_id=site.id)
    if current_state.status == "running":
        raise HTTPException(
            status_code=409,
            detail=f"A sync is already in progress for site '{site.name}'.",
        )
    logger.info("user=%s triggered manual sync for site '%s'", user.username, site.name)
    background_tasks.add_task(
        sync_service.run_sync,
        import_config=req.import_config,
        import_gravity=req.import_gravity,
        import_dhcp_leases=req.import_dhcp_leases,
        run_gravity=req.run_gravity,
        site_id=site.id,
    )
    return SyncStatusResponse(status="running")
