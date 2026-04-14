"""Block/unblock domains via the master Pi-hole, then sync to replicas."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.pihole import PiholeInstance
from app.models.user import User
from app.services import client_manager
from app.services import sync_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/domains", tags=["domains"])


class DomainRequest(BaseModel):
    domain: str


async def _get_master(db: AsyncSession) -> PiholeInstance:
    result = await db.execute(
        select(PiholeInstance).where(PiholeInstance.is_master.is_(True), PiholeInstance.is_active.is_(True))
    )
    master = result.scalar_one_or_none()
    if master is None:
        raise HTTPException(status_code=503, detail="No active master Pi-hole instance configured.")
    return master


@router.get("/block/{domain:path}")
async def check_domain_blocked(
    domain: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Check whether a domain is currently in the exact deny list on the master Pi-hole."""
    domain = domain.strip().lower()
    master = await _get_master(db)
    try:
        client = await client_manager.get_client(master)
        blocked = await client.is_domain_blocked(domain)
        return {"domain": domain, "blocked": blocked}
    except Exception as exc:
        logger.error("Failed to check block status for %s: %s", domain, exc)
        raise HTTPException(status_code=502, detail=f"Failed to check block status: {exc}")


@router.post("/block")
async def block_domain(
    req: DomainRequest,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Add domain to the exact deny list on the master Pi-hole, then sync to replicas."""
    domain = req.domain.strip().lower()
    if not domain:
        raise HTTPException(status_code=422, detail="Domain must not be empty.")

    master = await _get_master(db)
    try:
        client = await client_manager.get_client(master)
        await client.block_domain(domain)
        logger.info("Blocked domain %s on master %s", domain, master.name)
    except Exception as exc:
        logger.error("Failed to block domain %s on master %s: %s", domain, master.name, exc)
        raise HTTPException(status_code=502, detail=f"Failed to block domain on master: {exc}")

    # Sync master → replicas so the new deny entry propagates immediately.
    if sync_service.get_state().status != "running":
        asyncio.create_task(
            sync_service.run_sync(import_config=False, import_gravity=True, run_gravity=True)
        )
    return Response(status_code=204)


@router.delete("/block/{domain:path}")
async def unblock_domain(
    domain: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove domain from the exact deny list on every active Pi-hole instance.

    A full sync is intentionally avoided here: run_sync always runs gravity on the
    master before exporting, which would re-add the domain to gravity.db from adlists
    and undo the unblock.  Instead we call unblock_domain directly on each instance so
    the change takes effect without any gravity refresh.
    """
    domain = domain.strip().lower()
    if not domain:
        raise HTTPException(status_code=422, detail="Domain must not be empty.")

    result = await db.execute(
        select(PiholeInstance).where(PiholeInstance.is_active.is_(True))
    )
    instances = list(result.scalars().all())
    if not instances:
        raise HTTPException(status_code=503, detail="No active Pi-hole instances configured.")

    errors: list[str] = []
    for inst in instances:
        try:
            client = await client_manager.get_client(inst)
            await client.unblock_domain(domain)
            logger.info("Unblocked domain %s on %s", domain, inst.name)
        except Exception as exc:
            logger.error("Failed to unblock domain %s on %s: %s", domain, inst.name, exc)
            errors.append(f"{inst.name}: {exc}")

    if errors and len(errors) == len(instances):
        raise HTTPException(
            status_code=502,
            detail=f"Failed to unblock on all instances: {'; '.join(errors)}",
        )

    return Response(status_code=204)
