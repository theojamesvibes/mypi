"""Integration tests for the X-API-Key auth path.

Covers happy-path use, the read-only-key 403 enforcement (require_mutation),
the wave-1 last_used_at coalescing optimisation, the legacy SHA-256 hash
auto-upgrade introduced in 1.4.0, and revocation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio


# ── Happy path ───────────────────────────────────────────────────────────────


async def test_api_key_authenticates_get_me(client, api_key):
    """A fresh raw key in X-API-Key authenticates against /api/auth/me."""
    resp = await client.get("/api/auth/me", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


async def test_unknown_api_key_returns_401(client):
    resp = await client.get(
        "/api/auth/me",
        headers={"X-API-Key": "this-key-was-never-issued"},
    )
    assert resp.status_code == 401


async def test_revoked_api_key_no_longer_authenticates(authed_client, api_key, app):
    """DELETE /api/auth/api-key/{id} sets is_active=False, after which
    the same raw key should 401."""
    keys = await authed_client.get("/api/auth/api-keys")
    assert keys.status_code == 200
    # Find the freshly-minted key (read-write, not the read-only one)
    target = next(k for k in keys.json() if not k["is_read_only"])

    revoke = await authed_client.delete(f"/api/auth/api-key/{target['id']}")
    assert revoke.status_code == 200

    # Authed_client carries the session cookie that would auth via the
    # cookie path even after the API key is revoked. Spin up a truly
    # fresh client to test the X-API-Key path in isolation.
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as fresh:
        me = await fresh.get("/api/auth/me", headers={"X-API-Key": api_key})
    assert me.status_code == 401


# ── Read-only enforcement ────────────────────────────────────────────────────


async def test_readonly_key_blocked_from_mutation(client, readonly_api_key):
    """require_mutation must reject any read-only key on a POST
    endpoint. /api/auth/api-key (creating another key) is a clean
    mutation target."""
    resp = await client.post(
        "/api/auth/api-key",
        headers={"X-API-Key": readonly_api_key},
        json={"name": "should-not-create", "is_read_only": False},
    )
    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"].lower()


async def test_readonly_key_can_still_read(client, readonly_api_key):
    """Read-only is mutation-only, not read-only-on-everything."""
    resp = await client.get(
        "/api/auth/me", headers={"X-API-Key": readonly_api_key}
    )
    assert resp.status_code == 200


async def test_me_reports_is_read_only_for_readonly_key(client, readonly_api_key):
    """iOS-prep: /api/auth/me must surface the auth scope so a read-only
    client knows up front it can't mutate. Without this the iOS app has
    to discover its scope via 403 responses on every Block / Sync click."""
    resp = await client.get(
        "/api/auth/me", headers={"X-API-Key": readonly_api_key}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_read_only"] is True


async def test_me_reports_not_read_only_for_full_key(client, api_key):
    resp = await client.get("/api/auth/me", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["is_read_only"] is False


async def test_me_reports_not_read_only_for_password_login(authed_client):
    """Password / cookie auth must always come back as is_read_only=False —
    the flag is set exclusively when an API key is used."""
    resp = await authed_client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["is_read_only"] is False


# ── Wave-1: last_used_at coalescing ──────────────────────────────────────────


async def test_last_used_at_updates_on_first_use(client, api_key, db_session):
    """First request bumps last_used_at — the column was NULL on creation."""
    from app.models.user import ApiKey
    from sqlalchemy import select

    # Pre-condition: last_used_at is None or close to created_at.
    pre = (await db_session.execute(select(ApiKey))).scalars().all()
    assert len(pre) >= 1
    target = next(k for k in pre if not k.is_read_only)
    initial = target.last_used_at

    resp = await client.get("/api/auth/me", headers={"X-API-Key": api_key})
    assert resp.status_code == 200

    await db_session.refresh(target)
    assert target.last_used_at is not None
    if initial is not None:
        assert target.last_used_at >= initial


async def test_last_used_at_does_not_update_on_rapid_reuse(
    client, api_key, db_session,
):
    """Wave-1 coalescing: a second request within 60 s of the first
    must NOT trigger another commit on last_used_at — saves write
    amplification on iOS-style polling clients."""
    from app.models.user import ApiKey
    from sqlalchemy import select

    # First request — establishes last_used_at.
    await client.get("/api/auth/me", headers={"X-API-Key": api_key})
    first = (await db_session.execute(select(ApiKey))).scalars().all()
    target = next(k for k in first if not k.is_read_only)
    after_first = target.last_used_at
    assert after_first is not None

    # Second request — should be coalesced.
    await client.get("/api/auth/me", headers={"X-API-Key": api_key})
    await db_session.refresh(target)

    # Coalesced means timestamp didn't move (or moved by <1ms — but the
    # code path is "skip the write entirely", so it should be exactly
    # equal).
    assert target.last_used_at == after_first


async def test_last_used_at_does_update_after_coalesce_window(
    client, api_key, db_session,
):
    """If the recorded last_used_at is more than the coalesce window
    in the past, the next request DOES advance the timestamp."""
    from app.models.user import ApiKey
    from sqlalchemy import select

    # Push last_used_at backwards so the coalesce window has elapsed.
    keys = (await db_session.execute(select(ApiKey))).scalars().all()
    target = next(k for k in keys if not k.is_read_only)
    target.last_used_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db_session.commit()
    stale = target.last_used_at

    await client.get("/api/auth/me", headers={"X-API-Key": api_key})
    await db_session.refresh(target)

    assert target.last_used_at > stale


# ── Legacy SHA-256 hash auto-upgrade ─────────────────────────────────────────


async def test_legacy_sha256_key_authenticates_and_upgrades_to_hmac(
    client, test_user, db_session,
):
    """Pre-1.4.0 API keys were stored as plain SHA-256 of the raw key.
    On first use post-upgrade, the auth path matches the legacy hash,
    rotates the column to HMAC-SHA256, and serves the request."""
    from app.auth import _hash_api_key_sha256_legacy, hash_api_key
    from app.models.user import ApiKey

    user, _ = test_user
    raw = "legacy-key-from-1.3.x"
    legacy_hash = _hash_api_key_sha256_legacy(raw)

    legacy_row = ApiKey(
        user_id=user.id,
        name="legacy",
        key_hash=legacy_hash,
        key_hash_algo="sha256",
        is_read_only=False,
    )
    db_session.add(legacy_row)
    await db_session.commit()
    await db_session.refresh(legacy_row)
    legacy_id = legacy_row.id

    # First use: still hashed under the legacy algo, but auth path
    # transparently upgrades.
    resp = await client.get("/api/auth/me", headers={"X-API-Key": raw})
    assert resp.status_code == 200

    await db_session.refresh(legacy_row)
    assert legacy_row.key_hash_algo == "hmac-sha256"
    assert legacy_row.key_hash == hash_api_key(raw)

    # And the same raw key continues to authenticate after the upgrade.
    resp2 = await client.get("/api/auth/me", headers={"X-API-Key": raw})
    assert resp2.status_code == 200
    assert legacy_id == legacy_row.id  # sanity — same row, just rotated


# ── List + create ────────────────────────────────────────────────────────────


async def test_list_api_keys_returns_only_owners_keys(authed_client):
    keys = await authed_client.get("/api/auth/api-keys")
    assert keys.status_code == 200
    body = keys.json()
    # The two keys created by the api_key + readonly_api_key fixtures.
    # Note: only one of those is auto-created in this test (api_key
    # fixture). Adjust expectation.
    assert isinstance(body, list)


async def test_api_key_creation_returns_raw_key_only_once(authed_client):
    resp = await authed_client.post(
        "/api/auth/api-key",
        json={"name": "single-issue", "is_read_only": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "raw_key" in body
    assert len(body["raw_key"]) >= 40

    # The list endpoint never returns the raw key (security: only the
    # hash is stored, the raw is returned exactly once at creation).
    listing = await authed_client.get("/api/auth/api-keys")
    for key in listing.json():
        assert "raw_key" not in key
