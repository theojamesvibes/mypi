"""Tests for app/services/config_loader.py.

`sync_sites_and_instances` is the lifespan-startup bootstrap that
reconciles `pihole_instances.yml` with the DB on every container
start. It was 12% covered before this file; the orphan-detection,
main-reassignment, and rename paths run on every restart and have
been the source of multiple production incidents.

Tests monkeypatch `load_site_configs` to return SiteConfig lists
directly so they don't depend on YAML file I/O.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select


@pytest.fixture(autouse=True)
def _ensure_fernet_key():
    """sync_sites_and_instances writes the (encrypted) api_password,
    which needs an active Fernet key."""
    from cryptography.fernet import Fernet
    from app.config import settings
    import app.models.pihole as pihole_models

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None
    yield


def _site_cfg(name, slug, main=False, instances=()):
    from app.config import SiteConfig
    return SiteConfig(name=name, slug=slug, main=main, instances=list(instances))


def _inst_cfg(name, url, master=False, vip_role=None, password=""):
    from app.config import PiholeInstanceConfig
    return PiholeInstanceConfig(
        name=name, url=url, password=password, color="#3c8dbc",
        master=master, vip_role=vip_role,
    )


# ── Initial run ──────────────────────────────────────────────────────────────


async def test_initial_sync_creates_sites_and_instances(db_session, monkeypatch):
    from app.services import config_loader
    from app.models.pihole import PiholeInstance
    from app.models.site import Site

    site = _site_cfg("Main", "main", main=True, instances=[
        _inst_cfg("p1", "http://10.0.0.1", master=True),
        _inst_cfg("p2", "http://10.0.0.2"),
    ])
    monkeypatch.setattr(
        config_loader, "load_site_configs", lambda: [site],
    )

    await config_loader.sync_sites_and_instances(db_session)

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh:
        sites = (await fresh.execute(select(Site))).scalars().all()
        instances = (await fresh.execute(select(PiholeInstance))).scalars().all()

    assert len(sites) == 1
    assert sites[0].slug == "main"
    assert sites[0].is_main is True
    assert sites[0].is_active is True
    assert len(instances) == 2
    assert {i.name for i in instances} == {"p1", "p2"}
    master = next(i for i in instances if i.name == "p1")
    assert master.is_master is True


async def test_sync_is_idempotent(db_session, monkeypatch):
    """Re-running sync with the same YAML must not duplicate rows
    or flip flags."""
    from app.services import config_loader
    from app.models.pihole import PiholeInstance
    from app.models.site import Site

    site = _site_cfg("Main", "main", main=True, instances=[
        _inst_cfg("p1", "http://10.0.0.1", master=True),
    ])
    monkeypatch.setattr(
        config_loader, "load_site_configs", lambda: [site],
    )

    await config_loader.sync_sites_and_instances(db_session)
    await config_loader.sync_sites_and_instances(db_session)
    await config_loader.sync_sites_and_instances(db_session)

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh:
        sites = (await fresh.execute(select(Site))).scalars().all()
        instances = (await fresh.execute(select(PiholeInstance))).scalars().all()
    assert len(sites) == 1
    assert len(instances) == 1


# ── Orphan detection ─────────────────────────────────────────────────────────


async def test_removed_instance_marked_inactive(db_session, monkeypatch):
    """An instance present in the previous YAML but absent in the
    current one is marked is_active=False (not deleted)."""
    from app.services import config_loader
    from app.models.pihole import PiholeInstance

    initial = _site_cfg("Main", "main", main=True, instances=[
        _inst_cfg("p1", "http://10.0.0.1", master=True),
        _inst_cfg("p2", "http://10.0.0.2"),
    ])
    monkeypatch.setattr(
        config_loader, "load_site_configs", lambda: [initial],
    )
    await config_loader.sync_sites_and_instances(db_session)

    # Now the YAML lost p2.
    reduced = _site_cfg("Main", "main", main=True, instances=[
        _inst_cfg("p1", "http://10.0.0.1", master=True),
    ])
    monkeypatch.setattr(
        config_loader, "load_site_configs", lambda: [reduced],
    )
    await config_loader.sync_sites_and_instances(db_session)

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh:
        instances = (await fresh.execute(select(PiholeInstance))).scalars().all()
    by_name = {i.name: i for i in instances}
    assert by_name["p1"].is_active is True
    assert by_name["p2"].is_active is False  # orphaned


# ── Reactivation ─────────────────────────────────────────────────────────────


async def test_returning_instance_is_reactivated(db_session, monkeypatch):
    """An instance that was orphaned and is then re-added to the YAML
    flips back to is_active=True without losing its stats history."""
    from app.services import config_loader
    from app.models.pihole import PiholeInstance

    full = _site_cfg("Main", "main", main=True, instances=[
        _inst_cfg("p1", "http://10.0.0.1", master=True),
        _inst_cfg("p2", "http://10.0.0.2"),
    ])
    reduced = _site_cfg("Main", "main", main=True, instances=[
        _inst_cfg("p1", "http://10.0.0.1", master=True),
    ])
    monkeypatch.setattr(config_loader, "load_site_configs", lambda: [full])
    await config_loader.sync_sites_and_instances(db_session)
    monkeypatch.setattr(config_loader, "load_site_configs", lambda: [reduced])
    await config_loader.sync_sites_and_instances(db_session)
    monkeypatch.setattr(config_loader, "load_site_configs", lambda: [full])
    await config_loader.sync_sites_and_instances(db_session)

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh:
        instances = (await fresh.execute(select(PiholeInstance))).scalars().all()
    p2 = next(i for i in instances if i.name == "p2")
    assert p2.is_active is True


# ── Main reassignment ────────────────────────────────────────────────────────


async def test_main_reassignment_carries_settings_to_new_main(
    db_session, monkeypatch,
):
    """When the YAML's Main flag moves to a different site, the old
    Main's site_settings are materialised into the new Main where
    that key is NULL/missing — so non-Main sites' inheritance still
    resolves correctly."""
    from app.services import config_loader
    from app.models.site import Site, SiteSetting

    # Start: site_a is Main, site_b is not.
    monkeypatch.setattr(config_loader, "load_site_configs", lambda: [
        _site_cfg("A", "site-a", main=True, instances=[]),
        _site_cfg("B", "site-b", instances=[]),
    ])
    await config_loader.sync_sites_and_instances(db_session)

    # Seed a setting on the old Main.
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        a = (await session.execute(
            select(Site).where(Site.slug == "site-a")
        )).scalar_one()
        session.add(SiteSetting(site_id=a.id, key="test_key", value="test_value"))
        await session.commit()

    # Flip Main flag in YAML.
    monkeypatch.setattr(config_loader, "load_site_configs", lambda: [
        _site_cfg("A", "site-a", instances=[]),
        _site_cfg("B", "site-b", main=True, instances=[]),
    ])
    await config_loader.sync_sites_and_instances(db_session)

    async with AsyncSessionLocal() as session:
        sites = {s.slug: s for s in (
            await session.execute(select(Site))
        ).scalars().all()}
        assert sites["site-a"].is_main is False
        assert sites["site-b"].is_main is True

        b_setting = (await session.execute(
            select(SiteSetting).where(
                SiteSetting.site_id == sites["site-b"].id,
                SiteSetting.key == "test_key",
            )
        )).scalar_one_or_none()
    assert b_setting is not None
    assert b_setting.value == "test_value"


# ── Main-rename in place ─────────────────────────────────────────────────────


async def test_main_site_rename_preserves_data_via_slug_history(
    db_session, monkeypatch,
):
    """When the YAML's only Main site has a different name+slug than
    the DB's Main and no other site claims the new slug, treat as a
    rename: stash the old slug in slug_history and update in place."""
    from app.services import config_loader
    from app.models.site import Site, SiteSlugHistory
    from app.database import AsyncSessionLocal

    monkeypatch.setattr(config_loader, "load_site_configs", lambda: [
        _site_cfg("Default", "default", main=True, instances=[]),
    ])
    await config_loader.sync_sites_and_instances(db_session)

    async with AsyncSessionLocal() as s:
        original = (
            await s.execute(select(Site).where(Site.slug == "default"))
        ).scalar_one()
    original_id = original.id

    # Rename.
    monkeypatch.setattr(config_loader, "load_site_configs", lambda: [
        _site_cfg("WTR", "wtr", main=True, instances=[]),
    ])
    await config_loader.sync_sites_and_instances(db_session)

    async with AsyncSessionLocal() as s:
        sites = (await s.execute(select(Site))).scalars().all()
        history = (await s.execute(select(SiteSlugHistory))).scalars().all()

    # Same row, new slug + name.
    assert len(sites) == 1
    assert sites[0].id == original_id
    assert sites[0].slug == "wtr"
    assert sites[0].name == "WTR"
    # Slug history captures the old slug.
    assert any(h.old_slug == "default" for h in history)


# ── Empty YAML ───────────────────────────────────────────────────────────────


async def test_sync_with_no_sites_is_noop(db_session, monkeypatch):
    """Empty / blank YAML returns early without touching the DB."""
    from app.services import config_loader
    from app.models.site import Site

    monkeypatch.setattr(config_loader, "load_site_configs", lambda: [])
    await config_loader.sync_sites_and_instances(db_session)

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        sites = (await s.execute(select(Site))).scalars().all()
    assert sites == []
