"""Shared Pi-hole client registry.

Maintains persistent, authenticated PiholeClients per active instance, split
across independent *channels* so different callers don't share one TCP/TLS
connection.  The collector uses separate `stats` and `queries` channels — a
single dead keepalive connection can only wedge one poll path at a time, and
the other channel's success resets the per-instance circuit breaker.  Sync,
domain lookups, and version checks use the `default` channel, which is also
the only channel that restores/persists `pihole_instances.session_sid` to the
DB (avoids channels racing to overwrite each other's SID).
"""
from __future__ import annotations

import logging

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.services.pihole_client import PiholeClient

logger = logging.getLogger(__name__)

# One open client per (str(instance.id), channel).
_clients: dict[tuple[str, str], PiholeClient] = {}


async def get_client(instance: PiholeInstance, channel: str = "default") -> PiholeClient:
    """Return a persistent, open PiholeClient for *instance* on *channel*.

    Creates and opens a new client on first call per (instance, channel).  Only
    the ``default`` channel restores the persisted SID — other channels auth
    fresh on first use so they hold their own independent sessions without
    racing to overwrite the DB-persisted SID.
    """
    key = (str(instance.id), channel)
    if key not in _clients:
        client = PiholeClient(instance.url, instance.api_password)
        await client.open()
        if channel == "default" and instance.session_sid:
            client.sid = instance.session_sid
            logger.info("Restored session SID for %s (%s) — skipping auth", instance.name, channel)
        else:
            logger.info("Opened new client for %s (%s)", instance.name, channel)
        _clients[key] = client
    return _clients[key]


async def save_sid(instance_id: object, sid: str | None) -> None:
    """Persist the session SID so it survives container restarts.

    Only call this from the ``default`` channel — stats/queries channels have
    their own SIDs and writing those would stomp on the default channel's SID
    that sync/domains/version depend on across restarts.
    """
    async with AsyncSessionLocal() as db:
        inst = await db.get(PiholeInstance, instance_id)
        if inst and inst.session_sid != sid:
            inst.session_sid = sid
            await db.commit()


async def close_client(instance_id: object, channel: str = "default") -> None:
    """Close one channel's client for an instance (used by eviction path)."""
    key = (str(instance_id), channel)
    client = _clients.pop(key, None)
    if client:
        await client.close()


async def close_instance(instance_id: object) -> None:
    """Close ALL channels for an instance — used when an instance is removed."""
    iid = str(instance_id)
    for key in [k for k in _clients if k[0] == iid]:
        client = _clients.pop(key, None)
        if client:
            await client.close()


async def close_all_clients() -> None:
    """Call on shutdown to cleanly close all persistent HTTP clients."""
    for key in list(_clients):
        client = _clients.pop(key, None)
        if client:
            await client.close()
