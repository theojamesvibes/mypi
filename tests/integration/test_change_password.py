"""Integration tests for the change-password flow.

Covers the JSON endpoint validations (current-password challenge,
length, mismatch) and confirms that the old password no longer
authenticates after a successful change.
"""
from __future__ import annotations


async def test_change_password_succeeds_with_valid_inputs(authed_client, client, test_user):
    user, old_pw = test_user
    new_pw = "new-password-123"

    resp = await authed_client.post(
        "/api/auth/change-password",
        json={
            "current_password": old_pw,
            "new_password": new_pw,
            "confirm_password": new_pw,
        },
    )
    assert resp.status_code == 200

    # Old password no longer logs in.
    bad = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": old_pw},
    )
    assert bad.status_code == 401

    # New password does.
    good = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": new_pw},
    )
    assert good.status_code == 200


async def test_change_password_rejects_wrong_current_password(authed_client):
    resp = await authed_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "this-is-not-the-current-pw",
            "new_password": "new-password-123",
            "confirm_password": "new-password-123",
        },
    )
    assert resp.status_code == 422
    assert "current password" in resp.json()["detail"].lower()


async def test_change_password_rejects_short_new_password(authed_client, test_user):
    _, old_pw = test_user
    resp = await authed_client.post(
        "/api/auth/change-password",
        json={
            "current_password": old_pw,
            "new_password": "short",
            "confirm_password": "short",
        },
    )
    assert resp.status_code == 422
    assert "at least 8" in resp.json()["detail"].lower()


async def test_change_password_rejects_mismatched_confirm(authed_client, test_user):
    _, old_pw = test_user
    resp = await authed_client.post(
        "/api/auth/change-password",
        json={
            "current_password": old_pw,
            "new_password": "valid-password-1",
            "confirm_password": "valid-password-2",
        },
    )
    assert resp.status_code == 422
    assert "do not match" in resp.json()["detail"].lower()


async def test_change_password_clears_change_required_flag(
    authed_client, test_user, db_session,
):
    """After a successful change, password_change_required must be
    cleared so the user isn't prompted again on the next login."""
    from app.database import AsyncSessionLocal
    from app.models.user import User
    from sqlalchemy import select

    user, old_pw = test_user
    user.password_change_required = True
    db_session.add(user)
    await db_session.commit()

    resp = await authed_client.post(
        "/api/auth/change-password",
        json={
            "current_password": old_pw,
            "new_password": "new-password-456",
            "confirm_password": "new-password-456",
        },
    )
    assert resp.status_code == 200

    # The endpoint commits in its own session; reading via db_session's
    # identity map would return the stale Python object. Open a fresh
    # session so the SELECT actually round-trips to Postgres.
    async with AsyncSessionLocal() as fresh:
        refreshed = (
            await fresh.execute(select(User).where(User.id == user.id))
        ).scalar_one()
    assert refreshed.password_change_required is False
