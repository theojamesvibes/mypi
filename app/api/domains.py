"""Domain allow/deny list management — per-instance direct API calls, no sync needed.

Two router families are exposed, mirroring the stats/queries split:

  - global  (``/api/domains/...``)            — fleet-wide: mutations fan out to
    every active instance across all sites; the status read aggregates across
    all active masters (one per site) so a multi-site deployment doesn't crash.
  - per-site (``/api/sites/{slug}/domains/...``) — scoped to a single site's
    instances (and that site's single master for the status read).

The per-site routes back the domain modal on per-site pages; the global routes
back the all-sites pages, where there is no single master.
"""
import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._site_dep import resolve_site
from app.api.stats import GRAVITY_STATUSES, _list_label
from app.auth import get_current_user, require_mutation
from app.database import get_db
from app.limiter import limiter
from app.models.pihole import PiholeInstance, PiholeList, QueryLog
from app.models.site import Site
from app.models.user import User
from app.schemas.stats import DomainBlocklistEntry, DomainBlocklistsResponse
from app.services import client_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/domains", tags=["domains"])
site_router = APIRouter(prefix="/api/sites/{slug}/domains", tags=["domains (per-site)"])

# Allow only letters, digits, dot, underscore, hyphen and '*' (wildcards) —
# rejects anything that couldn't be a valid domain / list entry.
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9._*-]+$")


def _validate_domain(domain: str) -> None:
    if not domain or not _DOMAIN_RE.match(domain):
        raise HTTPException(status_code=422, detail="Invalid domain name.")


class DomainRequest(BaseModel):
    domain: str


async def _get_masters(db: AsyncSession, site_id: uuid.UUID | None) -> list[PiholeInstance]:
    """Active master instance(s). One per site — so an unscoped lookup in a
    multi-site deployment returns several, which is fine here: the status read
    ORs membership across them."""
    q = select(PiholeInstance).where(
        PiholeInstance.is_master.is_(True), PiholeInstance.is_active.is_(True)
    )
    if site_id is not None:
        q = q.where(PiholeInstance.site_id == site_id)
    masters = list((await db.execute(q)).scalars().all())
    if not masters:
        raise HTTPException(status_code=503, detail="No active master Pi-hole instance configured.")
    return masters


async def _get_all_active(db: AsyncSession, site_id: uuid.UUID | None) -> list[PiholeInstance]:
    q = select(PiholeInstance).where(PiholeInstance.is_active.is_(True))
    if site_id is not None:
        q = q.where(PiholeInstance.site_id == site_id)
    instances = list((await db.execute(q)).scalars().all())
    if not instances:
        raise HTTPException(status_code=503, detail="No active Pi-hole instances configured.")
    return instances


async def _scope_note(db: AsyncSession, site: Site) -> dict:
    """For a site-scoped mutation, list the *other* sites that have active
    instances so the UI can remind the user the change didn't reach them.

    Returns ``{"site_name": ..., "other_sites": [{"slug", "name"}, ...]}``.
    An empty ``other_sites`` (single-site deployment) means no reminder needed.
    """
    # sort_order is in the SELECT list because Postgres requires ORDER BY columns
    # to appear there under SELECT DISTINCT.
    q = (
        select(Site.slug, Site.name, Site.sort_order)
        .join(PiholeInstance, PiholeInstance.site_id == Site.id)
        .where(
            Site.is_active.is_(True),
            Site.id != site.id,
            PiholeInstance.is_active.is_(True),
        )
        .distinct()
        .order_by(Site.sort_order, Site.name)
    )
    others = [{"slug": slug, "name": name} for slug, name, _ in (await db.execute(q)).all()]
    return {"site_name": site.name, "other_sites": others}


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


# ─── Shared request bodies (site_id=None → global/fleet-wide) ─────────────────

async def _status_body(db: AsyncSession, domain: str, site_id: uuid.UUID | None) -> dict:
    """Check deny/allow list membership. Aggregates across the in-scope master(s)
    — a single site has one master; the global scope ORs across all sites' masters.

    Returns effective status: 'allowed' (allow list entry present, beats deny/gravity),
    'denied' (deny list only), or 'unmanaged' (not in any local list).
    """
    domain = domain.strip().lower()
    _validate_domain(domain)
    masters = await _get_masters(db, site_id)

    async def _check(master: PiholeInstance) -> dict[str, bool]:
        client = await client_manager.get_client(master)
        return await client.get_domain_list_status(domain)

    try:
        results = await asyncio.gather(*[_check(m) for m in masters])
    except Exception as exc:
        logger.exception("Failed to check domain status for %s: %s", domain, exc)
        raise HTTPException(
            status_code=502, detail="Failed to check domain status on master Pi-hole.",
        ) from exc

    in_deny = any(r["in_deny"] for r in results)
    in_allow = any(r["in_allow"] for r in results)
    if in_allow:
        effective = "allowed"
    elif in_deny:
        effective = "denied"
    else:
        effective = "unmanaged"

    return {"domain": domain, "in_deny": in_deny, "in_allow": in_allow, "effective": effective}


async def _deny_body(db: AsyncSession, domain: str, site_id: uuid.UUID | None, user: User) -> dict:
    domain = domain.strip().lower()
    _validate_domain(domain)
    instances = await _get_all_active(db, site_id)

    async def _deny(client, inst):
        await client.remove_allow_exact(domain)
        await client.add_deny_exact(domain)

    result = await _apply_to_all(instances, _deny)
    logger.info("user=%s added %s to deny list (ok=%d/%d)", user.username, domain, result["ok_count"], result["total"])
    if result["ok_count"] == 0:
        raise HTTPException(status_code=502, detail="Operation failed on all instances.")
    return result


async def _remove_deny_body(db: AsyncSession, domain: str, site_id: uuid.UUID | None, user: User) -> dict:
    domain = domain.strip().lower()
    _validate_domain(domain)
    instances = await _get_all_active(db, site_id)

    result = await _apply_to_all(instances, lambda client, inst: client.remove_deny_exact(domain))
    logger.info("user=%s removed %s from deny list (ok=%d/%d)", user.username, domain, result["ok_count"], result["total"])
    if result["ok_count"] == 0:
        raise HTTPException(status_code=502, detail="Operation failed on all instances.")
    return result


async def _allow_body(db: AsyncSession, domain: str, site_id: uuid.UUID | None, user: User) -> dict:
    domain = domain.strip().lower()
    _validate_domain(domain)
    instances = await _get_all_active(db, site_id)

    async def _allow(client, inst):
        await client.remove_deny_exact(domain)
        await client.add_allow_exact(domain)

    result = await _apply_to_all(instances, _allow)
    logger.info("user=%s added %s to allow list (ok=%d/%d)", user.username, domain, result["ok_count"], result["total"])
    if result["ok_count"] == 0:
        raise HTTPException(status_code=502, detail="Operation failed on all instances.")
    return result


async def _remove_allow_body(db: AsyncSession, domain: str, site_id: uuid.UUID | None, user: User) -> dict:
    domain = domain.strip().lower()
    _validate_domain(domain)
    instances = await _get_all_active(db, site_id)

    result = await _apply_to_all(instances, lambda client, inst: client.remove_allow_exact(domain))
    logger.info("user=%s removed %s from allow list (ok=%d/%d)", user.username, domain, result["ok_count"], result["total"])
    if result["ok_count"] == 0:
        raise HTTPException(status_code=502, detail="Operation failed on all instances.")
    return result


async def _blocklists_body(
    db: AsyncSession, domain: str, site_id: uuid.UUID | None, since: datetime
) -> DomainBlocklistsResponse:
    """Which list(s) block *domain* — answered by the in-scope master's live
    /api/search (Pi-hole's per-query list_id doesn't attribute gravity blocks).
    Adds a reliable in-window block count from our own query logs for context.
    """
    domain = domain.strip().lower()
    _validate_domain(domain)
    master = (await _get_masters(db, site_id))[0]  # gravity lists are consistent within scope

    try:
        client = await client_manager.get_client(master)
        search = await client.search_domain(domain)
    except Exception as exc:
        logger.exception("Domain search failed on master %s: %s", master.name, exc)
        raise HTTPException(status_code=502, detail="Failed to search domain on master Pi-hole.") from exc

    gravity = [g for g in (search.get("gravity") or []) if g.get("type") == "block"]
    denies = [d for d in (search.get("domains") or []) if d.get("type") == "deny"]

    # Flag which matched adlists are security feeds, from our mirror of this master.
    ids = [g["id"] for g in gravity if g.get("id") is not None]
    sec: dict[int, bool] = {}
    if ids:
        rows = (await db.execute(
            select(PiholeList.pihole_list_id, PiholeList.is_security).where(
                PiholeList.instance_id == master.id,
                PiholeList.list_type == "block",
                PiholeList.pihole_list_id.in_(ids),
            )
        )).all()
        sec = {pid: bool(is_sec) for pid, is_sec in rows}

    entries = [
        DomainBlocklistEntry(
            # Prefer the adlist's Pi-hole comment (the user's short name), fall
            # back to a label derived from the source URL.
            name=(g.get("comment") or "").strip() or _list_label(g.get("address")),
            address=g.get("address"),
            kind="gravity",
            enabled=bool(g.get("enabled", True)),
            is_security=sec.get(g.get("id"), False),
        )
        for g in gravity
    ]
    entries += [
        DomainBlocklistEntry(
            name=d.get("domain") or domain,
            address=None,
            kind="deny-regex" if d.get("kind") == "regex" else "deny-exact",
            enabled=bool(d.get("enabled", True)),
            is_security=False,
        )
        for d in denies
    ]

    # Context: how often this domain was actually gravity-blocked in the window.
    inst_ids = [i.id for i in await _get_all_active(db, site_id)]
    block_count = 0
    if inst_ids:
        block_count = (await db.execute(
            select(func.count(QueryLog.id)).where(
                QueryLog.domain == domain,
                QueryLog.timestamp >= since,
                QueryLog.status.in_(list(GRAVITY_STATUSES)),
                QueryLog.instance_id.in_(inst_ids),
            )
        )).scalar() or 0

    return DomainBlocklistsResponse(domain=domain, block_count=block_count, lists=entries)


def _since_from(hours: int, since: datetime | None) -> datetime:
    if since is not None:
        return since if since.tzinfo else since.replace(tzinfo=UTC)
    return datetime.now(UTC) - timedelta(hours=hours)


# ─── Global routes (fleet-wide: all active instances / all masters) ───────────

@router.get("/status/{domain:path}")
async def get_domain_status(
    domain: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _status_body(db, domain, site_id=None)


@router.post("/deny")
@limiter.limit("30/minute")
async def add_to_deny(
    request: Request,
    req: DomainRequest,
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add domain to exact deny list on all instances. Removes allow override first."""
    return await _deny_body(db, req.domain, None, user)


@router.delete("/deny/{domain:path}")
@limiter.limit("30/minute")
async def remove_from_deny(
    request: Request,
    domain: str,
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove domain from exact deny list on all instances."""
    return await _remove_deny_body(db, domain, None, user)


@router.post("/allow")
@limiter.limit("30/minute")
async def add_to_allow(
    request: Request,
    req: DomainRequest,
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add domain to exact allow list on all instances (overrides gravity/deny). Removes deny entry first."""
    return await _allow_body(db, req.domain, None, user)


@router.delete("/allow/{domain:path}")
@limiter.limit("30/minute")
async def remove_from_allow(
    request: Request,
    domain: str,
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove domain from exact allow list on all instances."""
    return await _remove_allow_body(db, domain, None, user)


@router.get("/blocklists/{domain:path}", response_model=DomainBlocklistsResponse)
@limiter.limit("30/minute")
async def get_domain_blocklists(
    request: Request,
    domain: str,
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DomainBlocklistsResponse:
    """Which list(s) block a domain — fleet-wide (searches any active master)."""
    return await _blocklists_body(db, domain, None, _since_from(hours, since))


# ─── Per-site routes (scoped to one site's instances / master) ────────────────

@site_router.get("/status/{domain:path}")
async def get_domain_status_for_site(
    domain: str,
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _status_body(db, domain, site_id=site.id)


@site_router.post("/deny")
@limiter.limit("30/minute")
async def add_to_deny_for_site(
    request: Request,
    req: DomainRequest,
    site: Site = Depends(resolve_site),
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    res = await _deny_body(db, req.domain, site.id, user)
    res.update(await _scope_note(db, site))
    return res


@site_router.delete("/deny/{domain:path}")
@limiter.limit("30/minute")
async def remove_from_deny_for_site(
    request: Request,
    domain: str,
    site: Site = Depends(resolve_site),
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    res = await _remove_deny_body(db, domain, site.id, user)
    res.update(await _scope_note(db, site))
    return res


@site_router.post("/allow")
@limiter.limit("30/minute")
async def add_to_allow_for_site(
    request: Request,
    req: DomainRequest,
    site: Site = Depends(resolve_site),
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    res = await _allow_body(db, req.domain, site.id, user)
    res.update(await _scope_note(db, site))
    return res


@site_router.delete("/allow/{domain:path}")
@limiter.limit("30/minute")
async def remove_from_allow_for_site(
    request: Request,
    domain: str,
    site: Site = Depends(resolve_site),
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict:
    res = await _remove_allow_body(db, domain, site.id, user)
    res.update(await _scope_note(db, site))
    return res


@site_router.get("/blocklists/{domain:path}", response_model=DomainBlocklistsResponse)
@limiter.limit("30/minute")
async def get_domain_blocklists_for_site(
    request: Request,
    domain: str,
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DomainBlocklistsResponse:
    """Which list(s) block a domain — scoped to this site's master."""
    return await _blocklists_body(db, domain, site.id, _since_from(hours, since))
