"""FastAPI dependency for resolving a Site by URL slug.

Used by every per-site API route under `/api/sites/{slug}/...`. Looks up
the slug in `sites` first; on miss, checks `site_slug_history` and
returns an HTTP 301 to the current slug if the old slug is retired.
Unknown slugs → 404.
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.site import Site, SiteSlugHistory

logger = logging.getLogger(__name__)


async def resolve_site(
    request: Request,
    slug: str = Path(..., min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
) -> Site:
    """Resolve an active Site by slug, following slug_history for renames.

    - Active match → returns the Site.
    - History match → raises 301 with Location pointing at the same path
      rewritten to the current slug. Bookmarks and old scripts keep working.
    - No match → 404.
    """
    result = await db.execute(
        select(Site).where(Site.slug == slug, Site.is_active.is_(True))
    )
    site = result.scalar_one_or_none()
    if site is not None:
        return site

    history_result = await db.execute(
        select(SiteSlugHistory).where(SiteSlugHistory.old_slug == slug)
    )
    history = history_result.scalar_one_or_none()
    if history is not None:
        current = await db.execute(
            select(Site).where(Site.id == history.site_id, Site.is_active.is_(True))
        )
        current_site = current.scalar_one_or_none()
        if current_site is not None:
            # Rewrite the path so /api/sites/old/... → /api/sites/new/...
            # and /dashboard/old → /dashboard/new.
            new_path = request.url.path.replace(f"/{slug}", f"/{current_site.slug}", 1)
            new_url = request.url.replace(path=new_path)
            raise HTTPException(
                status_code=301,
                detail=f"Site slug '{slug}' has been renamed to '{current_site.slug}'.",
                headers={"Location": str(new_url)},
            )

    raise HTTPException(status_code=404, detail=f"Site '{slug}' not found")
