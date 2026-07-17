"""Site management API.

  GET  /api/sites                — list active sites
  GET  /api/sites/inactive       — list orphan (deactivated) sites
  GET  /api/sites/{slug}         — one site's detail + instance count
  PATCH /api/sites/{slug}        — rename or change slug (old slug → history)
  DELETE /api/sites/{slug}       — permanently delete a deactivated site
                                   (cascades to its instances, stats, queries,
                                   site_settings)
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._site_dep import resolve_site
from app.auth import get_current_user, require_mutation
from app.config import slugify, validate_slug
from app.database import get_db
from app.models.pihole import PiholeInstance
from app.models.site import Site, SiteSlugHistory
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sites", tags=["sites"])


class SiteSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_main: bool
    is_active: bool
    sort_order: int
    instance_count: int
    active_instance_count: int
    created_at: datetime | None = None


class PatchSiteRequest(BaseModel):
    name: str | None = None
    slug: str | None = None


async def _build_summary(db: AsyncSession, site: Site) -> SiteSummary:
    """Assemble a site's summary, counting its total and active instances."""
    total = await db.execute(
        select(func.count(PiholeInstance.id)).where(PiholeInstance.site_id == site.id)
    )
    active = await db.execute(
        select(func.count(PiholeInstance.id)).where(
            PiholeInstance.site_id == site.id,
            PiholeInstance.is_active.is_(True),
        )
    )
    return SiteSummary(
        id=site.id,
        name=site.name,
        slug=site.slug,
        is_main=site.is_main,
        is_active=site.is_active,
        sort_order=site.sort_order,
        instance_count=total.scalar_one(),
        active_instance_count=active.scalar_one(),
        created_at=site.created_at,
    )


@router.get("", response_model=list[SiteSummary])
async def list_sites(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SiteSummary]:
    """Every active site, ordered by sort_order then name."""
    result = await db.execute(
        select(Site)
        .where(Site.is_active.is_(True))
        .order_by(Site.sort_order, Site.name)
    )
    sites = list(result.scalars().all())
    return [await _build_summary(db, s) for s in sites]


@router.get("/inactive", response_model=list[SiteSummary])
async def list_orphan_sites(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SiteSummary]:
    """Deactivated sites — removed from pihole_instances.yml. Shown in the
    web UI's orphan-cleanup section and targetable by DELETE."""
    result = await db.execute(
        select(Site)
        .where(Site.is_active.is_(False))
        .order_by(Site.name)
    )
    sites = list(result.scalars().all())
    return [await _build_summary(db, s) for s in sites]


@router.get("/{slug}", response_model=SiteSummary)
async def get_site(
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SiteSummary:
    return await _build_summary(db, site)


@router.patch("/{slug}", response_model=SiteSummary)
async def patch_site(
    req: PatchSiteRequest,
    site: Site = Depends(resolve_site),
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> SiteSummary:
    """Rename a site or change its slug. Slug changes write the old slug
    into `site_slug_history` so bookmarks and scripts keep working via
    the resolve_site dependency's 301 redirect."""
    if req.name is not None and req.name != site.name:
        site.name = req.name

    if req.slug is not None and req.slug != site.slug:
        new_slug = req.slug.strip()
        # Allow an operator to type the display name and have it slugified.
        if new_slug != new_slug.lower() or " " in new_slug:
            new_slug = slugify(new_slug)
        try:
            validate_slug(new_slug, f"PATCH site '{site.name}'")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Reject if an active site already owns that slug.
        conflict = await db.execute(
            select(Site).where(
                Site.slug == new_slug,
                Site.is_active.is_(True),
                Site.id != site.id,
            )
        )
        if conflict.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Slug '{new_slug}' is already in use by another active site.",
            )

        # Stash the old slug so bookmarks survive.
        db.add(SiteSlugHistory(
            old_slug=site.slug,
            site_id=site.id,
            retired_at=datetime.now(UTC),
        ))
        logger.info(
            "user=%s renaming slug for site '%s': %s → %s",
            user.username, site.name, site.slug, new_slug,
        )
        site.slug = new_slug

    await db.commit()
    await db.refresh(site)
    return await _build_summary(db, site)


@router.delete("/{slug}", status_code=204)
async def delete_orphan_site(
    slug: str,
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a deactivated site and cascade through its
    instances, stats snapshots, query logs, settings, and slug history.

    Only deactivated sites are deletable — active sites must be removed
    from pihole_instances.yml first (same rule as orphan instances).
    """
    result = await db.execute(select(Site).where(Site.slug == slug))
    site = result.scalar_one_or_none()
    if site is None:
        # Check slug_history in case the user hit the old URL.
        result = await db.execute(
            select(SiteSlugHistory).where(SiteSlugHistory.old_slug == slug)
        )
        history = result.scalar_one_or_none()
        if history is not None:
            raise HTTPException(
                status_code=410,
                detail=f"Slug '{slug}' was renamed and is no longer a direct target.",
            )
        raise HTTPException(status_code=404, detail=f"Site '{slug}' not found")

    if site.is_active:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an active site. Remove it from pihole_instances.yml first.",
        )

    site_name = site.name
    instance_count = await db.execute(
        select(func.count(PiholeInstance.id)).where(PiholeInstance.site_id == site.id)
    )
    inst_count = instance_count.scalar_one()

    await db.delete(site)
    await db.commit()
    logger.info(
        "user=%s permanently deleted orphan site id=%s name=%r slug=%r "
        "(%d instance(s) cascade-deleted)",
        user.username, site.id, site_name, slug, inst_count,
    )
