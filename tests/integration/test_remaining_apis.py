"""Tests for the smaller API surface — sites PATCH (rename + slug
history), api/_site_dep slug-history redirect, /api/auth session-
timeout + list-keys, /api/version + /api/display + /api/poll-settings."""
from __future__ import annotations

from datetime import UTC

import pytest

# ── /api/sites PATCH ─────────────────────────────────────────────────────────


@pytest.fixture
async def two_sites(db_session):
    from app.models.site import Site
    a = Site(name="A", slug="a", is_main=True, is_active=True, sort_order=0)
    b = Site(name="B", slug="b", is_main=False, is_active=True, sort_order=1)
    db_session.add_all([a, b])
    await db_session.commit()
    await db_session.refresh(a)
    await db_session.refresh(b)
    return a, b


async def test_patch_site_renames_in_place(authed_client, two_sites):
    a, _ = two_sites
    resp = await authed_client.patch(
        f"/api/sites/{a.slug}", json={"name": "A-Renamed"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "A-Renamed"


async def test_patch_site_changes_slug_and_writes_history(
    authed_client, two_sites, db_session,
):
    """Slug change writes the old slug into site_slug_history so
    bookmarks keep working via 301."""
    from sqlalchemy import select

    from app.models.site import SiteSlugHistory

    a, _ = two_sites
    old_slug = a.slug
    resp = await authed_client.patch(
        f"/api/sites/{old_slug}", json={"slug": "a-renamed"},
    )
    assert resp.status_code == 200

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh:
        history = (
            await fresh.execute(
                select(SiteSlugHistory).where(SiteSlugHistory.old_slug == old_slug)
            )
        ).scalars().all()
    assert len(history) == 1


async def test_patch_site_rejects_conflicting_slug(authed_client, two_sites):
    """If another active site already owns the requested slug,
    PATCH returns 409."""
    a, b = two_sites
    resp = await authed_client.patch(
        f"/api/sites/{a.slug}", json={"slug": b.slug},
    )
    assert resp.status_code == 409


async def test_patch_site_uppercase_slug_is_normalised(
    authed_client, two_sites,
):
    """An operator who types a display name (uppercase / spaces) gets
    it slugified rather than rejected — by design."""
    a, _ = two_sites
    resp = await authed_client.patch(
        f"/api/sites/{a.slug}", json={"slug": "New Name"},
    )
    # slugify("New Name") = "new-name" → valid → 200
    assert resp.status_code == 200
    assert resp.json()["slug"] == "new-name"


# ── api/_site_dep slug-history redirect ──────────────────────────────────────


async def test_old_slug_resolves_via_history_redirect(
    authed_client, two_sites, db_session,
):
    """Hitting /api/sites/{old_slug}/something after a rename returns
    a 301 redirect to the new slug — bookmarks survive renames."""
    from datetime import datetime

    from app.models.site import SiteSlugHistory

    a, _ = two_sites
    db_session.add(SiteSlugHistory(
        old_slug="legacy-a", site_id=a.id,
        retired_at=datetime.now(UTC),
    ))
    await db_session.commit()

    resp = await authed_client.get("/api/sites/legacy-a", follow_redirects=False)
    # resolve_site issues a 301 to the canonical slug.
    assert resp.status_code in (301, 308)


# ── /api/auth — session-timeout + list-keys ─────────────────────────────────


async def test_session_timeout_get_returns_value(authed_client):
    resp = await authed_client.get("/api/auth/session-timeout")
    assert resp.status_code == 200
    assert "timeout_minutes" in resp.json()


async def test_session_timeout_set_round_trip(authed_client):
    set_resp = await authed_client.put(
        "/api/auth/session-timeout", json={"timeout_minutes": 30},
    )
    assert set_resp.status_code == 200

    get_resp = await authed_client.get("/api/auth/session-timeout")
    assert get_resp.json()["timeout_minutes"] == 30


async def test_session_timeout_negative_rejected(authed_client):
    resp = await authed_client.put(
        "/api/auth/session-timeout", json={"timeout_minutes": -1},
    )
    assert resp.status_code == 422


async def test_list_api_keys_excludes_other_users_keys(
    authed_client, db_session, test_user,
):
    """User can only see their own keys, not other users'."""
    from app.auth import generate_api_key, hash_password
    from app.models.user import ApiKey, User

    other = User(
        username="bob", hashed_password=hash_password("p"),
        is_active=True, password_change_required=False,
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    _, h = generate_api_key()
    db_session.add(ApiKey(
        user_id=other.id, name="bob-key", key_hash=h,
        key_hash_algo="hmac-sha256", is_active=True, is_read_only=False,
    ))
    await db_session.commit()

    resp = await authed_client.get("/api/auth/api-keys")
    body = resp.json()
    # The fixture user is "alice" — bob's key must NOT be in the list.
    assert all(k["name"] != "bob-key" for k in body)


# ── /api/version ─────────────────────────────────────────────────────────────


async def test_version_status_endpoint(authed_client):
    resp = await authed_client.get("/api/version/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "current_version" in body
    assert "enabled" in body


async def test_version_settings_save(authed_client):
    resp = await authed_client.put(
        "/api/version/settings", json={"enabled": False},
    )
    assert resp.status_code == 200


async def test_version_check_endpoint_triggers_check(authed_client, respx_mock):
    """POST /api/version/check forces an immediate version_check.check_now."""
    respx_mock.get(
        "https://raw.githubusercontent.com/theojamesvibes/mypi/main/VERSION"
    ).respond(200, text="2.0.5\n")

    resp = await authed_client.post("/api/version/check")
    assert resp.status_code == 200


# ── /api/display-settings ────────────────────────────────────────────────────


async def test_display_settings_get_returns_current(authed_client, db_session):
    """Display settings live under Main — need a Main site."""
    from app.models.site import Site
    db_session.add(Site(
        name="M", slug="m", is_main=True, is_active=True, sort_order=0,
    ))
    await db_session.commit()

    resp = await authed_client.get("/api/display-settings")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


async def test_display_settings_round_trip(authed_client, db_session):
    from app.models.site import Site
    db_session.add(Site(
        name="M", slug="m", is_main=True, is_active=True, sort_order=0,
    ))
    await db_session.commit()

    resp = await authed_client.put(
        "/api/display-settings",
        json={"hide_pihole_self_in_top_clients": False},
    )
    assert resp.status_code == 200


# ── /api/poll-settings ───────────────────────────────────────────────────────


async def test_poll_settings_get(authed_client):
    # The router has a trailing-slash route — let httpx follow the
    # 307 if FastAPI issues one.
    resp = await authed_client.get("/api/poll-settings/", follow_redirects=True)
    assert resp.status_code == 200
    assert "interval_seconds" in resp.json()


async def test_poll_settings_put_round_trip(authed_client, db_session):
    from app.models.site import Site
    db_session.add(Site(
        name="M", slug="m", is_main=True, is_active=True, sort_order=0,
    ))
    await db_session.commit()

    resp = await authed_client.put(
        "/api/poll-settings/", json={"interval_seconds": 30},
        follow_redirects=True,
    )
    assert resp.status_code == 200


async def test_poll_settings_put_blocked_for_readonly_key(
    client, readonly_api_key,
):
    resp = await client.put(
        "/api/poll-settings/",
        headers={"X-API-Key": readonly_api_key},
        json={"interval_seconds": 30},
        follow_redirects=True,
    )
    assert resp.status_code == 403


# ── /api/instances DELETE for orphan ─────────────────────────────────────────


async def test_delete_inactive_instance_succeeds(authed_client, db_session):
    """An is_active=False instance can be permanently deleted via DELETE
    /api/instances/{id}."""
    from cryptography.fernet import Fernet

    import app.models.pihole as pihole_models
    from app.config import settings
    from app.models.pihole import PiholeInstance
    from app.models.site import Site

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    site = Site(name="Main", slug="main", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    inst = PiholeInstance(
        site_id=site.id, name="zombie", url="http://z",
        api_password="pw", is_active=False,
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)

    resp = await authed_client.delete(f"/api/instances/{inst.id}")
    assert resp.status_code == 204


async def test_delete_active_instance_returns_409(authed_client, db_session):
    """Active instances must be removed from YAML first."""
    from cryptography.fernet import Fernet

    import app.models.pihole as pihole_models
    from app.config import settings
    from app.models.pihole import PiholeInstance
    from app.models.site import Site

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    site = Site(name="Main", slug="main", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    inst = PiholeInstance(
        site_id=site.id, name="active", url="http://a",
        api_password="pw", is_active=True,
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)

    resp = await authed_client.delete(f"/api/instances/{inst.id}")
    assert resp.status_code == 409
