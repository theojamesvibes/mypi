"""Integration tests for app/services/client_manager.py.

Touches the real DB (PiholeInstance rows) so it lives in the
integration suite. respx is layered on top to stop the persistent
PiholeClient from making outbound network calls when get_client opens
a fresh httpx.AsyncClient.
"""
from __future__ import annotations

import uuid

import pytest
import respx
from httpx import Response


PIHOLE = "http://pihole.test"


@pytest.fixture
async def site(db_session):
    """Required by PiholeInstance via FK — every test needs a site row
    before it can persist an instance."""
    from app.models.site import Site

    s = Site(
        name="Test",
        slug="test",
        is_active=True,
        is_main=True,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def instance(db_session, site):
    """A persisted PiholeInstance pointing at the mock URL. The
    api_password column is Fernet-encrypted at rest, so an encryption
    key must be configured before the row is committed."""
    from cryptography.fernet import Fernet
    from app.config import settings
    from app.models.pihole import PiholeInstance
    import app.models.pihole as pihole_models

    # Ensure a Fernet key is available — pytest's app.config is built
    # at module load before this fixture sets one in the env, so set
    # it on the instance and clear the cached _fernet so the next
    # encrypt picks it up.
    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    inst = PiholeInstance(
        site_id=site.id,
        name="pihole-test",
        url=PIHOLE,
        api_password="pi-password",
        is_active=True,
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)
    return inst


@pytest.fixture(autouse=True)
async def _clear_client_registry():
    """The persistent client registry is module-level and would leak
    across tests without an explicit reset. Closing in the finally
    branch also evicts any open httpx clients."""
    from app.services import client_manager

    client_manager._clients.clear()
    client_manager._last_persisted_sid.clear()
    yield
    for key in list(client_manager._clients):
        try:
            await client_manager._clients[key].close()
        except Exception:
            pass
    client_manager._clients.clear()
    client_manager._last_persisted_sid.clear()


# ── get_client + caching ─────────────────────────────────────────────────────


async def test_get_client_creates_and_caches(instance):
    from app.services.client_manager import get_client

    first = await get_client(instance)
    second = await get_client(instance)
    # Same persistent client — no new session opened on Pi-hole's side.
    assert first is second


async def test_get_client_restores_persisted_sid(instance, db_session):
    """A SID stored on the row from a previous run is loaded into the
    in-memory client without re-authenticating. Saves a round-trip on
    container restart."""
    from app.models.pihole import PiholeInstance
    from app.services.client_manager import get_client
    from sqlalchemy import select

    instance.session_sid = "previously-persisted-sid"
    db_session.add(instance)
    await db_session.commit()

    # Re-fetch so the relationship matches what get_client sees.
    fresh = (
        await db_session.execute(
            select(PiholeInstance).where(PiholeInstance.id == instance.id)
        )
    ).scalar_one()
    client = await get_client(fresh)
    assert client.sid == "previously-persisted-sid"


# ── save_sid round-trip skip (wave-3) ────────────────────────────────────────


async def test_save_sid_writes_on_first_call(instance):
    """save_sid commits via its own session; verifying via the fixture's
    db_session would return the stale identity-mapped object. Use a
    fresh session so the SELECT actually round-trips to Postgres."""
    from app.database import AsyncSessionLocal
    from app.models.pihole import PiholeInstance
    from app.services.client_manager import save_sid
    from sqlalchemy import select

    await save_sid(instance.id, "fresh-sid-1")

    async with AsyncSessionLocal() as fresh:
        refreshed = (
            await fresh.execute(
                select(PiholeInstance).where(PiholeInstance.id == instance.id)
            )
        ).scalar_one()
    assert refreshed.session_sid == "fresh-sid-1"


async def test_save_sid_short_circuits_when_sid_unchanged(instance, db_session):
    """Wave-3 optimisation: when the new SID matches the one we last
    persisted, save_sid returns without opening a session — verified
    by mutating the DB row out-of-band and seeing that the next
    same-SID save_sid does NOT overwrite it."""
    from app.models.pihole import PiholeInstance
    from app.services.client_manager import save_sid
    from sqlalchemy import select

    # First save populates the in-memory cache for this instance.
    await save_sid(instance.id, "stable-sid")

    # Sneakily mutate the DB out-of-band to a different value.
    refreshed = (
        await db_session.execute(
            select(PiholeInstance).where(PiholeInstance.id == instance.id)
        )
    ).scalar_one()
    refreshed.session_sid = "out-of-band-edit"
    await db_session.commit()

    # Same SID as the cached one — save_sid should skip the DB entirely.
    await save_sid(instance.id, "stable-sid")

    # Verify by re-reading the row: if save_sid had touched the DB,
    # it would have overwritten our out-of-band edit. It didn't.
    again = (
        await db_session.execute(
            select(PiholeInstance).where(PiholeInstance.id == instance.id)
        )
    ).scalar_one()
    assert again.session_sid == "out-of-band-edit"


async def test_save_sid_writes_again_when_sid_rotates(instance):
    from app.database import AsyncSessionLocal
    from app.models.pihole import PiholeInstance
    from app.services.client_manager import save_sid
    from sqlalchemy import select

    await save_sid(instance.id, "old-sid")
    await save_sid(instance.id, "new-sid")  # genuine rotation

    async with AsyncSessionLocal() as fresh:
        refreshed = (
            await fresh.execute(
                select(PiholeInstance).where(PiholeInstance.id == instance.id)
            )
        ).scalar_one()
    assert refreshed.session_sid == "new-sid"


# ── close_client ─────────────────────────────────────────────────────────────


async def test_close_client_evicts_from_registry(instance, respx_mock):
    """Eviction without logout should not contact Pi-hole."""
    from app.services import client_manager

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "x"}}
    )
    client = await client_manager.get_client(instance)
    assert str(instance.id) in client_manager._clients

    await client_manager.close_client(str(instance.id), logout=False)
    assert str(instance.id) not in client_manager._clients


async def test_close_client_with_logout_clears_persisted_sid(
    instance, db_session, respx_mock,
):
    """Logout=True on close issues DELETE /api/auth and nulls the
    persisted SID column so a later startup doesn't try a stale one."""
    from app.models.pihole import PiholeInstance
    from app.services import client_manager
    from sqlalchemy import select

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "doomed-sid"}}
    )
    respx_mock.delete(f"{PIHOLE}/api/auth").respond(204)

    client = await client_manager.get_client(instance)
    await client._authenticate()
    await client_manager.save_sid(instance.id, "doomed-sid")

    await client_manager.close_client(str(instance.id), logout=True)

    refreshed = (
        await db_session.execute(
            select(PiholeInstance).where(PiholeInstance.id == instance.id)
        )
    ).scalar_one()
    assert refreshed.session_sid is None
