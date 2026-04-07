from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.stats import _latest_snapshots_by_instance
from app.auth import get_current_user
from app.database import get_db
from app.models.pihole import PiholeInstance
from app.models.user import User
from app.schemas.instance import InstanceStatus

router = APIRouter(prefix="/api/instances", tags=["instances"])


@router.get("", response_model=list[InstanceStatus])
async def list_instances(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PiholeInstance).order_by(PiholeInstance.name))
    instances = result.scalars().all()
    snapshots = await _latest_snapshots_by_instance(db)

    output = []
    for inst in instances:
        snap = snapshots.get(inst.id)
        output.append(InstanceStatus(
            id=inst.id,
            name=inst.name,
            url=inst.url,
            color=inst.color,
            is_active=inst.is_active,
            last_seen_at=inst.last_seen_at,
            status=snap.status if snap else "unknown",
            dns_queries_today=snap.dns_queries_today if snap else 0,
            queries_blocked=snap.queries_blocked if snap else 0,
            percent_blocked=snap.percent_blocked if snap else 0.0,
            domains_on_blocklist=snap.domains_on_blocklist if snap else 0,
            unique_clients=snap.unique_clients if snap else 0,
        ))
    return output
