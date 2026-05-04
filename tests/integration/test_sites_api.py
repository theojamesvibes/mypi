"""Tests for /api/sites — list, per-slug resolve, slug-history redirects."""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
async def two_sites(db_session):
    from app.models.site import Site
    main = Site(name="Main", slug="main-site", is_main=True, is_active=True, sort_order=0)
    other = Site(name="Other", slug="other", is_main=False, is_active=True, sort_order=1)
    db_session.add_all([main, other])
    await db_session.commit()
    await db_session.refresh(main)
    await db_session.refresh(other)
    return main, other


async def test_list_sites_requires_auth(client, two_sites):
    """No cookie, no API key → 401."""
    resp = await client.get("/api/sites")
    assert resp.status_code == 401


async def test_list_sites_returns_active_only(authed_client, db_session, two_sites):
    from app.models.site import Site

    # Add a deactivated site that should NOT appear in the list.
    inactive = Site(
        name="Old", slug="orphan", is_main=False, is_active=False, sort_order=99,
    )
    db_session.add(inactive)
    await db_session.commit()

    resp = await authed_client.get("/api/sites")
    assert resp.status_code == 200
    body = resp.json()
    slugs = {s["slug"] for s in body}
    assert "main-site" in slugs
    assert "other" in slugs
    assert "orphan" not in slugs


async def test_list_sites_returns_instance_counts(authed_client, db_session, two_sites):
    """Each row carries instance_count + active_instance_count so the UI
    can show counts without a per-row API roundtrip."""
    from cryptography.fernet import Fernet
    from app.config import settings
    from app.models.pihole import PiholeInstance
    import app.models.pihole as pihole_models

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    main, _ = two_sites
    db_session.add_all([
        PiholeInstance(
            site_id=main.id, name="active1", url="http://x1",
            api_password="p", is_active=True,
        ),
        PiholeInstance(
            site_id=main.id, name="inactive1", url="http://x2",
            api_password="p", is_active=False,
        ),
    ])
    await db_session.commit()

    resp = await authed_client.get("/api/sites")
    body = resp.json()
    main_row = next(s for s in body if s["slug"] == "main-site")
    assert main_row["instance_count"] == 2
    assert main_row["active_instance_count"] == 1


async def test_get_site_by_slug(authed_client, two_sites):
    resp = await authed_client.get("/api/sites/main-site")
    assert resp.status_code == 200
    assert resp.json()["slug"] == "main-site"
    assert resp.json()["is_main"] is True


async def test_get_site_unknown_slug_returns_404(authed_client):
    resp = await authed_client.get("/api/sites/nope-not-a-site")
    assert resp.status_code == 404


async def test_inactive_site_endpoint_returns_orphans(
    authed_client, db_session, two_sites,
):
    from app.models.site import Site
    db_session.add(Site(
        name="Removed", slug="removed", is_main=False,
        is_active=False, sort_order=99,
    ))
    await db_session.commit()

    resp = await authed_client.get("/api/sites/inactive")
    assert resp.status_code == 200
    slugs = {s["slug"] for s in resp.json()}
    assert "removed" in slugs
    # Active sites must NOT appear here.
    assert "main-site" not in slugs


async def test_delete_active_site_returns_409(authed_client, two_sites):
    """Active sites must be removed from pihole_instances.yml first."""
    resp = await authed_client.delete("/api/sites/main-site")
    assert resp.status_code == 409


async def test_delete_orphan_site_succeeds(authed_client, db_session, two_sites):
    from app.models.site import Site
    from sqlalchemy import select

    db_session.add(Site(
        name="Old", slug="old-orphan", is_main=False, is_active=False,
        sort_order=99,
    ))
    await db_session.commit()

    resp = await authed_client.delete("/api/sites/old-orphan")
    assert resp.status_code == 204

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh:
        survivor = (
            await fresh.execute(select(Site).where(Site.slug == "old-orphan"))
        ).scalar_one_or_none()
    assert survivor is None
