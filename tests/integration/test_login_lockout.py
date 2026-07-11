"""Brute-force lockout on /api/auth/login.

After `settings.login_lockout_threshold` consecutive failed logins for
a username (defaults to 5), that account is locked for
`settings.login_lockout_minutes` (defaults to 15). Subsequent attempts
return the same 401 / "Invalid credentials" response a wrong password
does — no enumeration leak.

A successful login resets the counter; a *partial* failure streak that
doesn't reach the threshold is also reset by the next success.

These tests run integration-style: real Postgres testcontainer, real
SQLAlchemy session, real bcrypt.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import settings

# ── Threshold + lockout window ──────────────────────────────────────────────


async def test_consecutive_failures_lock_the_account(client, test_user, db_session):
    user, _ = test_user
    threshold = settings.login_lockout_threshold

    # First N-1 wrong passwords return 401 but DON'T lock.
    for i in range(threshold - 1):
        resp = await client.post(
            "/api/auth/login",
            json={"username": user.username, "password": f"wrong-{i}"},
        )
        assert resp.status_code == 401

    # Re-fetch user state from the DB. Counter should equal N-1, no lockout.
    await db_session.refresh(user)
    assert user.failed_login_count == threshold - 1
    assert user.failed_login_lockout_until is None

    # The Nth wrong password trips the threshold and stamps a lockout.
    resp = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": "wrong-final"},
    )
    assert resp.status_code == 401

    await db_session.refresh(user)
    assert user.failed_login_count == threshold
    assert user.failed_login_lockout_until is not None
    assert user.failed_login_lockout_until > datetime.now(UTC)


async def test_locked_account_rejects_correct_password(
    client, test_user, db_session,
):
    """Once locked, even the *right* password returns 401. This is the
    load-bearing assertion — a lockout that can be bypassed with the
    correct password is no protection at all."""
    user, raw_password = test_user

    # Stamp a lockout directly so the test isn't sensitive to the
    # threshold setting.
    user.failed_login_lockout_until = datetime.now(UTC) + timedelta(minutes=15)
    user.failed_login_count = settings.login_lockout_threshold
    await db_session.commit()

    resp = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": raw_password},
    )
    assert resp.status_code == 401
    assert "session_token" not in resp.cookies


async def test_lockout_response_does_not_leak_state(
    client, test_user, db_session,
):
    """The 401 body for a locked-out account must be byte-identical to
    the 401 for a wrong password — otherwise an attacker discovers
    they've successfully locked a target account."""
    user, _ = test_user

    user.failed_login_lockout_until = datetime.now(UTC) + timedelta(minutes=15)
    await db_session.commit()

    locked = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": "anything"},
    )
    wrong = await client.post(
        "/api/auth/login",
        json={"username": "ghost-user", "password": "anything"},
    )

    # We compare the JSON detail rather than the full body because the
    # FastAPI HTTPException emitter is allowed to vary on charset.
    assert locked.status_code == wrong.status_code == 401
    assert locked.json() == wrong.json()


# ── Recovery / counter reset ────────────────────────────────────────────────


async def test_successful_login_resets_failure_counter(
    client, test_user, db_session,
):
    """A correct password mid-streak (i.e. before the threshold trips)
    should reset both fields. Otherwise a tired user typing one wrong
    password twice over weeks would slowly accumulate to a lockout."""
    user, raw_password = test_user

    # Two wrong passwords (under threshold).
    for i in range(2):
        await client.post(
            "/api/auth/login",
            json={"username": user.username, "password": f"wrong-{i}"},
        )
    await db_session.refresh(user)
    assert user.failed_login_count == 2

    # Now succeed.
    resp = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": raw_password},
    )
    assert resp.status_code == 200

    await db_session.refresh(user)
    assert user.failed_login_count == 0
    assert user.failed_login_lockout_until is None


async def test_lockout_clears_after_window_expires(
    client, test_user, db_session,
):
    """A lockout in the past is treated as not-locked, and a correct
    password during that window logs in successfully."""
    user, raw_password = test_user

    user.failed_login_lockout_until = datetime.now(UTC) - timedelta(minutes=1)
    user.failed_login_count = settings.login_lockout_threshold
    await db_session.commit()

    resp = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": raw_password},
    )
    assert resp.status_code == 200

    await db_session.refresh(user)
    assert user.failed_login_count == 0
    assert user.failed_login_lockout_until is None


# ── Unknown-username path doesn't pollute any user's counter ───────────────


async def test_unknown_username_does_not_increment_other_counters(
    client, test_user, db_session,
):
    """A wrong-username attempt must not touch any *other* user's
    failure counter — otherwise an attacker hitting random usernames
    could lock real accounts they don't even know exist."""
    user, _ = test_user

    initial_count = user.failed_login_count
    for _ in range(settings.login_lockout_threshold + 2):
        resp = await client.post(
            "/api/auth/login",
            json={"username": "no-such-user", "password": "whatever"},
        )
        assert resp.status_code == 401

    await db_session.refresh(user)
    assert user.failed_login_count == initial_count
    assert user.failed_login_lockout_until is None
