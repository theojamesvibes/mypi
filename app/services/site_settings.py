"""Per-site settings with Main-fallback inheritance.

A site_settings row with value=NULL (or no row at all) means "inherit from
the currently-active Main site." Main itself never inherits — its row's
value is authoritative, and if missing, the caller's hard-coded default
applies.

This module is available for any service that needs to read or write a
per-site setting. Phase 2 adds the module; Phase 4 migrates the existing
readers (pushover, poll_settings, sync_service) onto it.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.site import Site, SiteSetting

logger = logging.getLogger(__name__)


async def get_main_site_id(db: AsyncSession) -> uuid.UUID | None:
    """Return the id of the currently-active Main site, or None if there isn't one."""
    result = await db.execute(
        select(Site.id).where(Site.is_main.is_(True), Site.is_active.is_(True))
    )
    return result.scalar_one_or_none()


async def get_setting(
    db: AsyncSession,
    site_id: uuid.UUID,
    key: str,
    default: str | None = None,
) -> str | None:
    """Resolve a per-site setting with Main fallback.

    - Row present with non-NULL value → returns that value (even empty string).
    - Row missing OR value=NULL → falls back to the active Main site.
    - If Main also has no value → returns `default`.
    - If this site IS Main → no fallback; a missing row returns `default`.
    """
    result = await db.execute(
        select(SiteSetting.value).where(
            SiteSetting.site_id == site_id,
            SiteSetting.key == key,
        )
    )
    value = result.scalar_one_or_none()
    if value is not None:
        return value

    main_id = await get_main_site_id(db)
    if main_id is None or main_id == site_id:
        return default

    result = await db.execute(
        select(SiteSetting.value).where(
            SiteSetting.site_id == main_id,
            SiteSetting.key == key,
        )
    )
    main_value = result.scalar_one_or_none()
    return main_value if main_value is not None else default


async def set_setting(
    db: AsyncSession,
    site_id: uuid.UUID,
    key: str,
    value: str | None,
) -> None:
    """Upsert a per-site setting. Passing value=None stores NULL, meaning
    "inherit from Main" on subsequent reads (equivalent to clear_setting)."""
    stmt = (
        pg_insert(SiteSetting)
        .values(site_id=site_id, key=key, value=value)
        .on_conflict_do_update(
            index_elements=["site_id", "key"],
            set_={"value": value},
        )
    )
    await db.execute(stmt)
    await db.commit()

    # Verify the write, matching the established pattern from sync_service /
    # pushover — catch silent session-level write failures early.
    result = await db.execute(
        select(SiteSetting).where(
            SiteSetting.site_id == site_id,
            SiteSetting.key == key,
        )
    )
    row = result.scalar_one_or_none()
    if row is None or row.value != value:
        raise RuntimeError(
            f"site_settings upsert verification failed: site_id={site_id} "
            f"key={key!r} expected={value!r} "
            f"got={'nothing' if row is None else repr(row.value)}"
        )
    logger.debug(
        "Saved site_settings site_id=%s key=%s (value present=%s)",
        site_id, key, value is not None,
    )


async def clear_setting(db: AsyncSession, site_id: uuid.UUID, key: str) -> None:
    """Explicitly set value=NULL so reads fall through to Main."""
    await set_setting(db, site_id, key, None)


async def get_json_setting(
    db: AsyncSession,
    site_id: uuid.UUID,
    key: str,
    default: Any = None,
) -> Any:
    """Convenience wrapper — same as get_setting but JSON-decodes the value
    on read. Returns `default` when nothing is set (at this site or Main)."""
    import json
    raw = await get_setting(db, site_id, key, default=None)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "site_settings site_id=%s key=%s stores invalid JSON; returning default",
            site_id, key,
        )
        return default


async def set_json_setting(
    db: AsyncSession, site_id: uuid.UUID, key: str, value: Any,
) -> None:
    """Convenience wrapper — JSON-encodes `value` and stores it."""
    import json
    await set_setting(db, site_id, key, json.dumps(value))
