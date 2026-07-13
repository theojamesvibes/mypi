"""Tests for app/main.py web-UI routes — the Jinja-rendered pages
behind /, /dashboard, /queries, /settings, /combined, /change-password,
/login, /logout.

The integration suite already covers /api/* — these tests pin the HTML
routes' status codes, redirects-when-not-authed, and presence of a few
recognisable bytes in the response body.
"""
from __future__ import annotations

# ── Anonymous → /login redirect ──────────────────────────────────────────────


async def test_root_redirects_to_login_when_anonymous(client):
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login")


async def test_dashboard_redirects_to_login_when_anonymous(client):
    resp = await client.get("/dashboard/main", follow_redirects=False)
    assert resp.status_code == 303


async def test_queries_redirects_to_login_when_anonymous(client):
    resp = await client.get("/queries", follow_redirects=False)
    assert resp.status_code == 303


async def test_settings_redirects_to_login_when_anonymous(client):
    resp = await client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303


async def test_combined_redirects_to_login_when_anonymous(client):
    resp = await client.get("/combined", follow_redirects=False)
    assert resp.status_code == 303


async def test_change_password_redirects_to_login_when_anonymous(client):
    resp = await client.get("/change-password", follow_redirects=False)
    assert resp.status_code == 303


# ── /login GET ───────────────────────────────────────────────────────────────


async def test_login_page_renders_html(client):
    resp = await client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert b"<form" in resp.content


# ── Authenticated routes render ──────────────────────────────────────────────


async def test_root_renders_dashboard_when_authed(authed_client):
    resp = await authed_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_dashboard_for_site_renders_when_authed(authed_client):
    resp = await authed_client.get("/dashboard/some-slug")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_queries_renders_when_authed(authed_client):
    resp = await authed_client.get("/queries")
    assert resp.status_code == 200


async def test_queries_for_site_renders_when_authed(authed_client):
    resp = await authed_client.get("/queries/some-slug")
    assert resp.status_code == 200


async def test_settings_renders_when_authed(authed_client):
    resp = await authed_client.get("/settings")
    assert resp.status_code == 200


async def test_settings_for_site_renders_when_authed(authed_client):
    resp = await authed_client.get("/settings/some-slug")
    assert resp.status_code == 200


async def test_combined_renders_when_authed(authed_client):
    resp = await authed_client.get("/combined")
    assert resp.status_code == 200


async def test_change_password_renders_when_authed(authed_client):
    resp = await authed_client.get("/change-password")
    assert resp.status_code == 200
    assert b"<form" in resp.content


# ── /login POST ──────────────────────────────────────────────────────────────


async def test_login_form_post_with_valid_creds_redirects_home(client, test_user):
    user, password = test_user
    resp = await client.post(
        "/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    # Cookie set so the next request is authenticated.
    assert "session_token" in resp.cookies


async def test_login_form_post_with_invalid_creds_returns_401_html(
    client, test_user,
):
    user, _ = test_user
    resp = await client.post(
        "/login",
        data={"username": user.username, "password": "WRONG"},
    )
    assert resp.status_code == 401
    assert b"Invalid username or password" in resp.content


async def test_login_form_post_with_password_change_required_redirects_to_change(
    client, test_user, db_session,
):
    """A user flagged for forced password change is sent to
    /change-password instead of /."""
    from sqlalchemy import select

    from app.models.user import User

    user, password = test_user
    fresh = (
        await db_session.execute(select(User).where(User.id == user.id))
    ).scalar_one()
    fresh.password_change_required = True
    await db_session.commit()

    resp = await client.post(
        "/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"


# ── /logout ──────────────────────────────────────────────────────────────────


async def test_logout_redirects_to_login_and_clears_cookie(authed_client):
    resp = await authed_client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_logout_rejects_get(authed_client):
    """Logout mutates state (JTI revocation), so it must not be reachable
    via GET — SameSite=Lax cookies accompany cross-site top-level GET
    navigations, which would let any page log the user out via a link."""
    resp = await authed_client.get("/logout", follow_redirects=False)
    assert resp.status_code == 405


# ── /change-password POST validation ─────────────────────────────────────────


async def test_change_password_form_short_returns_422(authed_client, test_user):
    _, old_pw = test_user
    resp = await authed_client.post(
        "/change-password",
        data={
            "current_password": old_pw,
            "new_password": "short",
            "confirm_password": "short",
        },
    )
    assert resp.status_code == 422
    assert b"at least 8" in resp.content.lower() or b"at least" in resp.content


async def test_change_password_form_mismatch_returns_422(authed_client, test_user):
    _, old_pw = test_user
    resp = await authed_client.post(
        "/change-password",
        data={
            "current_password": old_pw,
            "new_password": "newpass-12345",
            "confirm_password": "DIFFERENT-12345",
        },
    )
    assert resp.status_code == 422


async def test_change_password_form_wrong_current_returns_422(
    authed_client,
):
    resp = await authed_client.post(
        "/change-password",
        data={
            "current_password": "wrong",
            "new_password": "newpass-12345",
            "confirm_password": "newpass-12345",
        },
    )
    assert resp.status_code == 422


async def test_change_password_form_rejects_readonly_api_key(
    client, readonly_api_key, test_user,
):
    """Regression: the form route used to accept any authenticated
    principal — a read-only API key could change the account password,
    bypassing the read-only guarantee its JSON twin enforces."""
    _, old_pw = test_user
    resp = await client.post(
        "/change-password",
        headers={"X-API-Key": readonly_api_key},
        data={
            "current_password": old_pw,
            "new_password": "newpass-12345",
            "confirm_password": "newpass-12345",
        },
    )
    assert resp.status_code == 403


async def test_change_password_form_success_redirects_home(
    authed_client, test_user,
):
    _, old_pw = test_user
    resp = await authed_client.post(
        "/change-password",
        data={
            "current_password": old_pw,
            "new_password": "newpass-12345",
            "confirm_password": "newpass-12345",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
