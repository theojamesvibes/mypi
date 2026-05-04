"""Shared Pi-hole client registry.

Maintains one persistent, authenticated PiholeClient per active instance.
Both the collector (polling) and sync service borrow clients from here rather
than creating throwaway sessions on every operation.
"""
from __future__ import annotations

import logging
import uuid

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.services.pihole_client import PiholeClient

logger = logging.getLogger(__name__)

# One open client per instance (keyed by str(instance.id)).
_clients: dict[str, PiholeClient] = {}

# Last SID we actually persisted to the DB, keyed by str(instance.id).
# `save_sid` skips the SELECT/UPDATE round-trip when the new SID matches
# what's already cached here. After a process restart this cache is
# empty, so the first save per instance does pay one round-trip — but
# that confirms the in-memory cache and the DB row, after which the
# poll path stops touching the DB on every successful auth.
_last_persisted_sid: dict[str, str | None] = {}

# Sentinel that's neither None nor any valid SID — used so the
# "haven't seen this instance yet" case forces one DB round-trip after
# a restart while a real `None` sid (no auth) short-circuits normally.
_SENTINEL = object()


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
    """Persist the session SID so it survives container restarts.

    Short-circuits when *sid* matches the value already persisted, so the
    common case (poll succeeded, SID hasn't rotated) costs nothing.
    """
    key = str(instance_id)
    if _last_persisted_sid.get(key, _SENTINEL) == sid:
        return
    async with AsyncSessionLocal() as db:
        inst = await db.get(PiholeInstance, instance_id)
        if inst and inst.session_sid != sid:
            inst.session_sid = sid
            await db.commit()
        _last_persisted_sid[key] = sid


async def close_client(instance_key: str, *, logout: bool = False) -> None:
    """Close and drop the cached client for *instance_key*.

    ``logout=False`` (default) is the transient-eviction path: the SID stays
    valid on Pi-hole and the next :func:`get_client` will reuse it from the DB.
    ``logout=True`` is for shutdown or instance removal — issue DELETE /api/auth
    so Pi-hole releases the session slot, then clear the persisted SID so a
    later startup doesn't try a stale one.
    """
    client = _clients.pop(instance_key, None)
    if client:
        await client.close(logout=logout)
    if logout:
        # Force the persisted-SID cache to miss so the DB row is cleared
        # even if the cached value already happens to be None.
        _last_persisted_sid.pop(instance_key, None)
        try:
            await save_sid(uuid.UUID(instance_key), None)
        except Exception as exc:
            logger.debug("Clearing persisted SID for %s failed (non-fatal): %s", instance_key, exc)


async def close_all_clients() -> None:
    """Call on shutdown to cleanly close all persistent HTTP clients."""
    for key in list(_clients):
        await close_client(key, logout=True)
