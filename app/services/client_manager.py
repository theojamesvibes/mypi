"""Shared Pi-hole client registry.

Maintains one persistent, authenticated PiholeClient per active instance.
Both the collector (polling) and sync service borrow clients from here rather
than creating throwaway sessions on every operation.
"""
from __future__ import annotations

import logging

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.services.pihole_client import PiholeClient

logger = logging.getLogger(__name__)

# One open client per instance (keyed by str(instance.id)).
_clients: dict[str, PiholeClient] = {}


async def get_client(instance: PiholeInstance) -> PiholeClient:
    """Return a persistent, open PiholeClient for *instance*.

    Creates and opens a new client on first call. On subsequent calls the same
    client is returned, preserving the authenticated session so Pi-hole doesn't
    accumulate stale sessions.  If a SID was previously persisted to the DB it
    is restored so we skip a round-trip auth on restart.
    """
    key = str(instance.id)
    if key not in _clients:
        client = PiholeClient(instance.url, instance.api_password)
        await client.open()
        if instance.session_sid:
            client.sid = instance.session_sid
            logger.info("Restored session SID for %s — skipping auth", instance.name)
        else:
            logger.info("Opened new client for %s", instance.name)
        _clients[key] = client
    return _clients[key]


async def save_sid(instance_id: object, sid: str | None) -> None:
    """Persist the session SID so it survives container restarts."""
    async with AsyncSessionLocal() as db:
        inst = await db.get(PiholeInstance, instance_id)
        if inst and inst.session_sid != sid:
            inst.session_sid = sid
            await db.commit()


async def close_client(instance_key: str) -> None:
    client = _clients.pop(instance_key, None)
    if client:
        await client.close()


async def close_all_clients() -> None:
    """Call on shutdown to cleanly close all persistent HTTP clients."""
    for key in list(_clients):
        await close_client(key)
