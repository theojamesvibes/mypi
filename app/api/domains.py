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


def _trigger_sync() -> None:
    if sync_service.get_state().status != "running":
        asyncio.create_task(
            sync_service.run_sync(import_config=False, import_gravity=True, run_gravity=True)
        )


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

    _trigger_sync()
    return Response(status_code=204)


@router.delete("/block/{domain:path}")
async def unblock_domain(
    domain: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove domain from the exact deny list on the master Pi-hole, then sync to replicas."""
    domain = domain.strip().lower()
    if not domain:
        raise HTTPException(status_code=422, detail="Domain must not be empty.")

    master = await _get_master(db)
    try:
        client = await client_manager.get_client(master)
        await client.unblock_domain(domain)
        logger.info("Unblocked domain %s on master %s", domain, master.name)
    except Exception as exc:
        logger.error("Failed to unblock domain %s on master %s: %s", domain, master.name, exc)
        raise HTTPException(status_code=502, detail=f"Failed to unblock domain on master: {exc}")

    _trigger_sync()
    return Response(status_code=204)
