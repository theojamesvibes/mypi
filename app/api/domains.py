"""Domain allow/deny list management — per-instance direct API calls, no sync needed."""
import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_mutation
from app.database import get_db
from app.limiter import limiter
from app.models.pihole import PiholeInstance
from app.models.user import User
from app.services import client_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/domains", tags=["domains"])

_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9._*-]+$")


def _validate_domain(domain: str) -> None:
    if not domain or not _DOMAIN_RE.match(domain):
        raise HTTPException(status_code=422, detail="Invalid domain name.")


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


async def _get_all_active(db: AsyncSession) -> list[PiholeInstance]:
    result = await db.execute(select(PiholeInstance).where(PiholeInstance.is_active.is_(True)))
    instances = list(result.scalars().all())
    if not instances:
        raise HTTPException(status_code=503, detail="No active Pi-hole instances configured.")
    return instances


async def _apply_to_all(instances: list[PiholeInstance], fn) -> dict:
    """Run fn(client, inst) on all instances concurrently. Returns per-instance results."""
    async def _run(inst):
        try:
            client = await client_manager.get_client(inst)
            await fn(client, inst)
            return {"name": inst.name, "ok": True}
        except Exception as exc:
            logger.error("Domain operation failed on %s: %s", inst.name, exc)
            return {"name": inst.name, "ok": False, "error": str(exc)}

    results = await asyncio.gather(*[_run(inst) for inst in instances])
    ok_count = sum(1 for r in results if r["ok"])
    return {"results": list(results), "ok_count": ok_count, "total": len(instances)}


@router.get("/status/{domain:path}")
async def get_domain_status(
    domain: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Check deny/allow list membership on the master Pi-hole.

    Returns effective status: 'allowed' (allow list entry present, beats deny/gravity),
    'denied' (deny list only), or 'unmanaged' (not in any local list).
    """
    domain = domain.strip().lower()
    _validate_domain(domain)
    master = await _get_master(db)
    try:
        client = await client_manager.get_client(master)
        raw = await client.get_domain_list_status(domain)
    except Exception as exc:
        logger.exception("Failed to check domain status on master %s: %s", master.name, exc)
        raise HTTPException(status_code=502, detail="Failed to check domain status on master Pi-hole.")

    in_deny = raw["in_deny"]
    in_allow = raw["in_allow"]
    if in_allow:
        effective = "allowed"
    elif in_deny:
        effective = "denied"
    else:
        effective = "unmanaged"

    return {"domain": domain, "in_deny": in_deny, "in_allow": in_allow, "effective": effective}


@router.post("/deny")
@limiter.limit("30/minute")
async def add_to_deny(
    request: Request,
    req: DomainRequest,
    _: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add domain to exact deny list on all instances. Removes allow override first."""
    domain = req.domain.strip().lower()
    _validate_domain(domain)
    instances = await _get_all_active(db)

    async def _deny(client, inst):
        await client.remove_allow_exact(domain)
        await client.add_deny_exact(domain)

    result = await _apply_to_all(instances, _deny)
    if result["ok_count"] == 0:
        raise HTTPException(status_code=502, detail="Operation failed on all instances.")
    return result


@router.delete("/deny/{domain:path}")
@limiter.limit("30/minute")
async def remove_from_deny(
    request: Request,
    domain: str,
    _: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove domain from exact deny list on all instances."""
    domain = domain.strip().lower()
    _validate_domain(domain)
    instances = await _get_all_active(db)

    result = await _apply_to_all(instances, lambda client, inst: client.remove_deny_exact(domain))
    if result["ok_count"] == 0:
        raise HTTPException(status_code=502, detail="Operation failed on all instances.")
    return result


@router.post("/allow")
@limiter.limit("30/minute")
async def add_to_allow(
    request: Request,
    req: DomainRequest,
    _: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add domain to exact allow list on all instances (overrides gravity/deny). Removes deny entry first."""
    domain = req.domain.strip().lower()
    _validate_domain(domain)
    instances = await _get_all_active(db)

    async def _allow(client, inst):
        await client.remove_deny_exact(domain)
        await client.add_allow_exact(domain)

    result = await _apply_to_all(instances, _allow)
    if result["ok_count"] == 0:
        raise HTTPException(status_code=502, detail="Operation failed on all instances.")
    return result


@router.delete("/allow/{domain:path}")
@limiter.limit("30/minute")
async def remove_from_allow(
    request: Request,
    domain: str,
    _: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove domain from exact allow list on all instances."""
    domain = domain.strip().lower()
    _validate_domain(domain)
    instances = await _get_all_active(db)

    result = await _apply_to_all(instances, lambda client, inst: client.remove_allow_exact(domain))
    if result["ok_count"] == 0:
        raise HTTPException(status_code=502, detail="Operation failed on all instances.")
    return result
