"""Adlist mirror job.

Mirrors each active instance's GET /api/lists into the `pihole_lists` table so
the dashboard can resolve a blocked query's `list_id` to a human name and flag
which lists are security/threat feeds. Lists change rarely, so this runs on a
slow interval (see `list_sync_interval_minutes`), separate from the hot
stats/queries polls.

`is_security` is computed here from Pi-hole group membership: an adlist assigned
to the group named by `settings.security_group_name` (case-insensitive) is a
threat feed. Only block-type adlists are stored — those are what gravity blocks
attribute to via `list_id`.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance, PiholeList
from app.services.client_manager import close_client, get_client, save_sid
from app.services.collector.instances import _get_active_instances

logger = logging.getLogger(__name__)


async def _sync_lists_for(instance: PiholeInstance) -> int:
    """Mirror one instance's block-type adlists. Returns the count stored."""
    try:
        client = await get_client(instance)
        groups = await client.get_groups()
        lists = await client.get_lists()
        await save_sid(instance.id, client.sid)
    except Exception as exc:
        logger.warning(
            "List sync failed for %s: %s: %s", instance.name, type(exc).__name__, exc
        )
        await close_client(str(instance.id))
        return 0

    # Resolve the configured security group to its id (blank name disables the
    # flag). Group names are unique in Pi-hole, so at most one matches.
    want = (settings.security_group_name or "").strip().lower()
    security_gids = {gid for gid, name in groups.items() if want and (name or "").strip().lower() == want}

    block_lists = [row for row in lists if row.get("type") == "block"]
    seen_ids: list[int] = []
    async with AsyncSessionLocal() as db:
        for row in block_lists:
            lid = row.get("id")
            if lid is None:
                continue
            seen_ids.append(lid)
            gids = set(row.get("groups") or [])
            is_security = bool(security_gids & gids)
            values = {
                "instance_id": instance.id,
                "pihole_list_id": lid,
                "list_type": "block",
                "address": (row.get("address") or "")[:512],
                "comment": (row.get("comment") or None),
                "enabled": bool(row.get("enabled", True)),
                "is_security": is_security,
                "updated_at": func.now(),
            }
            stmt = (
                pg_insert(PiholeList)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["instance_id", "pihole_list_id", "list_type"],
                    set_={
                        "address": values["address"],
                        "comment": values["comment"],
                        "enabled": values["enabled"],
                        "is_security": values["is_security"],
                        "updated_at": func.now(),
                    },
                )
            )
            await db.execute(stmt)

        # Drop rows for adlists removed from Pi-hole so stale names can't linger.
        prune = delete(PiholeList).where(
            PiholeList.instance_id == instance.id,
            PiholeList.list_type == "block",
        )
        if seen_ids:
            prune = prune.where(PiholeList.pihole_list_id.notin_(seen_ids))
        await db.execute(prune)
        await db.commit()

    logger.info(
        "Synced %d block adlists for %s (%d security)",
        len(seen_ids), instance.name,
        sum(1 for r in block_lists if security_gids & set(r.get("groups") or [])),
    )
    return len(seen_ids)


async def sync_all_lists() -> None:
    """Mirror block adlists for every active instance in parallel.

    A failure in one instance must never stop the others (per the collector's
    isolation rule), so each `_sync_lists_for` swallows and logs its own errors.
    """
    instances = await _get_active_instances()
    if not instances:
        return
    await asyncio.gather(*[_sync_lists_for(inst) for inst in instances])
