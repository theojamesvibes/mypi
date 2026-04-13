from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.stats import _latest_snapshots_by_instance
from app.auth import get_current_user
from app.database import get_db
from app.models.pihole import PiholeInstance
from app.models.user import User
from app.schemas.instance import ComponentVersionSchema, InstanceStatus, InstanceVersionInfo
from app.services import client_manager

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
        last_seen_at=inst.last_seen_at,
        status=snap.status if snap else "unknown",
        dns_queries_today=snap.dns_queries_today if snap else 0,
        queries_blocked=snap.queries_blocked if snap else 0,
        percent_blocked=snap.percent_blocked if snap else 0.0,
        domains_on_blocklist=snap.domains_on_blocklist if snap else 0,
        unique_clients=snap.unique_clients if snap else 0,
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


@router.get("/versions", response_model=list[InstanceVersionInfo])
async def list_instance_versions(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch installed/latest version info from all active Pi-hole instances concurrently."""
    result = await db.execute(
        select(PiholeInstance)
        .where(PiholeInstance.is_active.is_(True))
        .order_by(PiholeInstance.name)
    )
    instances = result.scalars().all()

    async def _fetch(inst: PiholeInstance) -> InstanceVersionInfo:
        try:
            client = await client_manager.get_client(inst)
            vi = await client.get_version_info()
            return InstanceVersionInfo(
                id=inst.id,
                name=inst.name,
                color=inst.color,
                is_master=inst.is_master,
                core=ComponentVersionSchema(
                    current=vi.core.current,
                    latest=vi.core.latest,
                    update_available=vi.core.update_available,
                ),
                ftl=ComponentVersionSchema(
                    current=vi.ftl.current,
                    latest=vi.ftl.latest,
                    update_available=vi.ftl.update_available,
                ),
                web=ComponentVersionSchema(
                    current=vi.web.current,
                    latest=vi.web.latest,
                    update_available=vi.web.update_available,
                ),
            )
        except Exception as exc:
            logger.warning("Failed to fetch version info for %s: %s", inst.name, exc)
            return InstanceVersionInfo(
                id=inst.id,
                name=inst.name,
                color=inst.color,
                is_master=inst.is_master,
                error=str(exc),
            )

    results = await asyncio.gather(*[_fetch(inst) for inst in instances])
    return list(results)


@router.delete("/{instance_id}", status_code=204)
async def delete_instance(
    instance_id: uuid.UUID,
    _: User = Depends(get_current_user),
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
    await db.delete(inst)
    await db.commit()
