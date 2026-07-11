"""DB lookup helpers shared by the collector's poll/backfill/version jobs."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.models.site import Site


async def _get_active_instances() -> list[PiholeInstance]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PiholeInstance).where(PiholeInstance.is_active.is_(True)))
        return list(result.scalars().all())


async def _get_instances_for_site(site_id: uuid.UUID) -> list[PiholeInstance]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PiholeInstance).where(
                PiholeInstance.site_id == site_id,
                PiholeInstance.is_active.is_(True),
            )
        )
        return list(result.scalars().all())


async def get_active_site_ids() -> list[uuid.UUID]:
    """Used by startup to register a poll-job pair per active site."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Site.id).where(Site.is_active.is_(True)).order_by(Site.sort_order, Site.name)
        )
        return [row[0] for row in result.fetchall()]


async def _get_site_name(site_id: uuid.UUID) -> str:
    """Look up a site's human-readable name for alert/log messages.
    Returns the stringified id if the site is missing (shouldn't happen;
    defensive fallback)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Site.name).where(Site.id == site_id))
        name = result.scalar_one_or_none()
    return name or str(site_id)
