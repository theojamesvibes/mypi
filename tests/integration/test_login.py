"""Integration tests for login + logout flows.

Covers both the JSON `/api/auth/login` endpoint used by the iOS app /
automation, the cookie behaviour on success, JTI revocation on logout,
and the wave-1 timing-equalisation fix.
"""
from __future__ import annotations

import time

# ── Happy path ───────────────────────────────────────────────────────────────


async def test_login_with_valid_credentials_returns_200(client, test_user):
    user, password = test_user
    resp = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": password},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and len(body["access_token"]) > 20


async def test_login_sets_session_cookie(client, test_user):
    user, password = test_user
    resp = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": password},
    )
    assert resp.status_code == 200
    cookie = resp.cookies.get("session_token")
    assert cookie is not None and len(cookie) > 20


async def test_session_cookie_authenticates_subsequent_requests(client, test_user):
    user, password = test_user
    await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": password},
    )
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == user.username


# ── Failure modes ────────────────────────────────────────────────────────────


async def test_login_with_wrong_password_returns_401(client, test_user):
    user, _ = test_user
    resp = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": "WRONG-password"},
    )
    assert resp.status_code == 401
    assert "session_token" not in resp.cookies


async def test_login_with_unknown_username_returns_401(client):
    resp = await client.post(
        "/api/auth/login",
        json={"username": "nobody-here", "password": "anything"},
    )
    assert resp.status_code == 401


async def test_login_with_inactive_user_returns_401(client, db_session):
    """An is_active=False row must not authenticate even with the right
    password — the SELECT filter in _user_by_username excludes them."""
    from app.auth import hash_password
    from app.models.user import User

    inactive = User(
        username="banned",
        hashed_password=hash_password("password"),
        is_active=False,
    )
    db_session.add(inactive)
    await db_session.commit()

    resp = await client.post(
        "/api/auth/login",
        json={"username": "banned", "password": "password"},
    )
    assert resp.status_code == 401


# ── Wave-1: timing equalisation ──────────────────────────────────────────────


async def test_login_unknown_user_runs_bcrypt_to_equalise_timing(client):
    """Wave-1 fix — when the username doesn't exist we still run bcrypt
    against a dummy hash so the response time doesn't reveal whether
    the username is registered. bcrypt verify on cost-12 is ~50–200 ms;
    a short-circuit would be sub-millisecond."""
    t0 = time.perf_counter()
    await client.post(
        "/api/auth/login",
        json={"username": "noone", "password": "x"},
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms >= 30, (
        f"login response took only {elapsed_ms:.1f}ms — short-circuit suspected"
    )


# ── Rate limiting ────────────────────────────────────────────────────────────


async def test_login_rate_limit_fires_after_10_per_minute(client, test_user):
    """slowapi caps /api/auth/login at 10/minute per source IP. Burst
    11 wrong-password attempts and confirm the 11th is 429."""
    user, _ = test_user
    for _ in range(10):
        resp = await client.post(
            "/api/auth/login",
            json={"username": user.username, "password": "wrong"},
        )
        # First 10 should be 401 (auth fail), not 429 (rate limited).
        assert resp.status_code == 401

    resp_11 = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": "wrong"},
    )
    assert resp_11.status_code == 429


# ── Logout + JTI revocation ──────────────────────────────────────────────────


async def test_logout_clears_session_cookie(authed_client):
    resp = await authed_client.post("/api/auth/logout")
    assert resp.status_code == 200
    # Cookie deletion: server returns Set-Cookie with empty value + past expiry.
    # Easiest check is that subsequent /me calls don't authenticate.
    me = await authed_client.get("/api/auth/me")
    assert me.status_code == 401


async def test_logout_revokes_jwt_so_bearer_use_fails(client, test_user):
    """Bearer-token reuse after logout must 401 — JTI is added to the
    revoked_tokens table and `_is_token_revoked` blocks the second use."""
    user, password = test_user
    login = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": password},
    )
    token = login.json()["access_token"]

    me_before = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me_before.status_code == 200

    # Use Authorization-header logout so the bearer JTI gets revoked
    # (the cookie path would only revoke the cookie's JTI).
    logout = await client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert logout.status_code == 200

    # Build a fresh client without the cookie this client also acquired,
    # so we test the bearer path in isolation.
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as fresh:
        me_after = await fresh.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
    assert me_after.status_code == 401
