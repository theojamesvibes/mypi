"""Pi-hole version fetch job — no stats snapshot, no query poll, no alerts."""
from __future__ import annotations

import asyncio
import logging

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.services import pihole_version_check
from app.services.client_manager import get_client, save_sid
from app.services.collector.instances import _get_active_instances

logger = logging.getLogger(__name__)


async def _fetch_version_for(instance: PiholeInstance) -> None:
    """Fetch and persist version info for a single instance (no stats, no alerts)."""
    try:
        client = await get_client(instance)
        version_info = await client.get_version_info()
        await save_sid(instance.id, client.sid)
        async with AsyncSessionLocal() as db:
            inst = await db.get(PiholeInstance, instance.id)
            if inst:
                inst.version_core = version_info.core.current or None
                inst.version_ftl = version_info.ftl.current or None
                inst.version_web = version_info.web.current or None
                inst.update_available_core = pihole_version_check.compute_update_available(inst.version_core, "core")
                inst.update_available_ftl = pihole_version_check.compute_update_available(inst.version_ftl, "ftl")
                inst.update_available_web = pihole_version_check.compute_update_available(inst.version_web, "web")
                await db.commit()
        logger.info("Fetched version info for %s: core=%s ftl=%s web=%s",
                    instance.name, version_info.core.current, version_info.ftl.current, version_info.web.current)
    except Exception as exc:
        logger.warning("Could not fetch version info for %s: %s", instance.name, exc)


async def fetch_all_instance_versions() -> None:
    """Fetch and persist Pi-hole version info for all active instances.

    Lightweight — no stats snapshot, no query poll, no alerts.
    Called at startup and after each sync so version data is always fresh.
    """
    instances = await _get_active_instances()
    await asyncio.gather(*[_fetch_version_for(inst) for inst in instances])
