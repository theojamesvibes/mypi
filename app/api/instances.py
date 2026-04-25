from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._site_dep import resolve_site
from app.api.stats import _latest_snapshots_by_instance
from app.auth import get_current_user, require_mutation
from app.database import get_db
from app.models.pihole import PiholeInstance
from app.models.site import Site
from app.models.user import User
from app.schemas.instance import InstanceStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/instances", tags=["instances"])


def _build_status(inst: PiholeInstance, snapshots: dict) -> InstanceStatus:
    snap = snapshots.get(inst.id)
    return InstanceStatus(
        id=inst.id,
        name=inst.name,
        url=inst.url,
        color=inst.color,
        is_active=inst.is_active,
        is_master=inst.is_master,
        vip_role=inst.vip_role,
        last_seen_at=inst.last_seen_at,
        status=snap.status if snap else "unknown",
        dns_queries_today=snap.dns_queries_today if snap else 0,
        queries_blocked=snap.queries_blocked if snap else 0,
        percent_blocked=snap.percent_blocked if snap else 0.0,
        domains_on_blocklist=snap.domains_on_blocklist if snap else 0,
        unique_clients=snap.unique_clients if snap else 0,
        version_core=inst.version_core,
        version_ftl=inst.version_ftl,
        version_web=inst.version_web,
        update_available_core=inst.update_available_core,
        update_available_ftl=inst.update_available_ftl,
        update_available_web=inst.update_available_web,
    )


@router.get("", response_model=list[InstanceStatus])
async def list_instances(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return only active (currently configured) instances."""
    result = await db.execute(
        select(PiholeInstance)
        .where(PiholeInstance.is_active.is_(True))
        .order_by(PiholeInstance.name)
    )
    instances = result.scalars().all()
    snapshots = await _latest_snapshots_by_instance(db)
    return [_build_status(inst, snapshots) for inst in instances]


@router.get("/stale", response_model=list[InstanceStatus])
async def list_stale_instances(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return instances that were removed from pihole_instances.yml (is_active=False)."""
    result = await db.execute(
        select(PiholeInstance)
        .where(PiholeInstance.is_active.is_(False))
        .order_by(PiholeInstance.name)
    )
    instances = result.scalars().all()
    snapshots = await _latest_snapshots_by_instance(db)
    return [_build_status(inst, snapshots) for inst in instances]


@router.delete("/{instance_id}", status_code=204)
async def delete_instance(
    instance_id: uuid.UUID,
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a stale instance and all its historical data.
    Only allowed for inactive instances (removed from pihole_instances.yml).
    """
    result = await db.execute(
        select(PiholeInstance).where(PiholeInstance.id == instance_id)
    )
    inst = result.scalar_one_or_none()
    if inst is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    if inst.is_active:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an active instance. Remove it from pihole_instances.yml first.",
        )
    inst_name = inst.name
    await db.delete(inst)
    await db.commit()
    logger.info("user=%s permanently deleted stale instance id=%s name=%r", user.username, instance_id, inst_name)


# ── Per-site variants ────────────────────────────────────────────────────────

site_router = APIRouter(prefix="/api/sites/{slug}/instances", tags=["instances (per-site)"])


@site_router.get("", response_model=list[InstanceStatus])
async def list_instances_for_site(
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Active instances belonging to one site."""
    result = await db.execute(
        select(PiholeInstance)
        .where(
            PiholeInstance.site_id == site.id,
            PiholeInstance.is_active.is_(True),
        )
        .order_by(PiholeInstance.name)
    )
    instances = result.scalars().all()
    snapshots = await _latest_snapshots_by_instance(db)
    return [_build_status(inst, snapshots) for inst in instances]


@site_router.get("/stale", response_model=list[InstanceStatus])
async def list_stale_instances_for_site(
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Instances under this site that were removed from pihole_instances.yml."""
    result = await db.execute(
        select(PiholeInstance)
        .where(
            PiholeInstance.site_id == site.id,
            PiholeInstance.is_active.is_(False),
        )
        .order_by(PiholeInstance.name)
    )
    instances = result.scalars().all()
    snapshots = await _latest_snapshots_by_instance(db)
    return [_build_status(inst, snapshots) for inst in instances]
