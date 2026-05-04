"""Tests for /api/notifications — Pushover settings round-trip,
mask-on-read, validate, test endpoint rate-limited."""
from __future__ import annotations

import pytest
import respx


@pytest.fixture
async def site(db_session):
    """Pushover settings persist into site_settings under the Main
    site, so we need at least one Site row for save_settings to work."""
    from app.models.site import Site
    s = Site(name="Main", slug="main", is_main=True, is_active=True, sort_order=0)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture(autouse=True)
def _reset_pushover_module(monkeypatch):
    """Pushover settings live in module globals — reset between tests
    so a save in one doesn't leak into another."""
    from app.services import pushover
    monkeypatch.setattr(pushover, "_app_token", "")
    monkeypatch.setattr(pushover, "_user_key", "")
    monkeypatch.setattr(pushover, "_enabled", False)
    yield


async def test_get_settings_unauthenticated_returns_401(client):
    resp = await client.get("/api/notifications/settings")
    assert resp.status_code == 401


async def test_save_and_get_settings_round_trip(authed_client, site):
    payload = {
        "app_token": "azGDORePK8gMaC0QOYAMyEEuzJnyUi",
        "user_key": "uQiRzpo4DXghDmr9QzzfQu27cmVRsG",
        "enabled": True,
        "alert_sync_failure": True,
        "alert_instance_offline": True,
        "alert_high_block_rate": False,
        "alert_on_vip_transfer": False,
        "block_rate_threshold_pct": 50.0,
        "offline_alert_max_count": 1,
        "offline_alert_retries": 1,
    }
    put = await authed_client.put("/api/notifications/settings", json=payload)
    assert put.status_code == 200
    body = put.json()
    # GET response masks the sensitive bits — only the last 4 chars survive.
    assert body["app_token"].startswith("****")
    assert body["app_token"].endswith(payload["app_token"][-4:])
    assert body["user_key"].startswith("****")


async def test_get_settings_masks_credentials(authed_client, site):
    """After save, retrieving the settings returns masked tokens — the
    raw credentials never leak via the API."""
    raw_token = "abcdef0123456789abcdef0123456789"
    raw_key = "0123456789abcdef0123456789abcdef"
    await authed_client.put("/api/notifications/settings", json={
        "app_token": raw_token, "user_key": raw_key, "enabled": True,
        "alert_sync_failure": True, "alert_instance_offline": True,
        "alert_high_block_rate": False, "alert_on_vip_transfer": False,
        "block_rate_threshold_pct": 50.0,
        "offline_alert_max_count": 1, "offline_alert_retries": 1,
    })
    resp = await authed_client.get("/api/notifications/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert raw_token not in body["app_token"]
    assert raw_key not in body["user_key"]
    # Last 4 chars survive for operator recognition.
    assert body["app_token"].endswith(raw_token[-4:])


async def test_validate_endpoint_calls_pushover(authed_client, respx_mock):
    """Validate POSTs to api.pushover.net/users/validate.json and
    returns whether the credentials were accepted."""
    respx_mock.post("https://api.pushover.net/1/users/validate.json").respond(
        200, json={"status": 1}
    )
    resp = await authed_client.post(
        "/api/notifications/validate",
        json={"app_token": "tok", "user_key": "key"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_validate_endpoint_reports_failure(authed_client, respx_mock):
    respx_mock.post("https://api.pushover.net/1/users/validate.json").respond(
        200, json={"status": 0, "errors": ["application token is invalid"]}
    )
    resp = await authed_client.post(
        "/api/notifications/validate",
        json={"app_token": "tok", "user_key": "key"},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "invalid" in body["error"].lower()


async def test_save_settings_blocked_on_readonly_key(
    client, readonly_api_key, site,
):
    resp = await client.put(
        "/api/notifications/settings",
        headers={"X-API-Key": readonly_api_key},
        json={
            "app_token": "x", "user_key": "y", "enabled": True,
            "alert_sync_failure": True, "alert_instance_offline": True,
            "alert_high_block_rate": False, "alert_on_vip_transfer": False,
            "block_rate_threshold_pct": 50.0,
            "offline_alert_max_count": 1, "offline_alert_retries": 1,
        },
    )
    assert resp.status_code == 403
