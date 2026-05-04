"""Advanced Pushover tests — exercises the _post HTTP path with respx
and the per-site config resolution that the basic test_pushover_service
file from PR 5 doesn't reach."""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _ensure_fernet_key():
    from cryptography.fernet import Fernet
    from app.config import settings
    import app.models.pihole as pihole_models

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None
    yield


@pytest.fixture(autouse=True)
def _reset_pushover_module(monkeypatch):
    from app.services import pushover
    monkeypatch.setattr(pushover, "_app_token", "")
    monkeypatch.setattr(pushover, "_user_key", "")
    monkeypatch.setattr(pushover, "_enabled", False)
    yield


# ── _post (the actual HTTP path) ─────────────────────────────────────────────


async def test_send_makes_http_call_when_enabled(respx_mock):
    """With creds + enabled, send() POSTs to api.pushover.net."""
    from app.services import pushover

    pushover._enabled = True
    pushover._app_token = "tok"
    pushover._user_key = "key"

    route = respx_mock.post(
        "https://api.pushover.net/1/messages.json"
    ).respond(200, json={"status": 1})

    ok = await pushover.send("hello", title="MyPi")
    assert ok is True
    assert route.call_count == 1


async def test_send_returns_false_on_pushover_error_response(respx_mock):
    """status != 1 in the response body means Pushover rejected the
    payload. send() returns False without raising."""
    from app.services import pushover

    pushover._enabled = True
    pushover._app_token = "tok"
    pushover._user_key = "key"

    respx_mock.post(
        "https://api.pushover.net/1/messages.json"
    ).respond(200, json={"status": 0, "errors": ["app token bad"]})

    ok = await pushover.send("hello")
    assert ok is False


async def test_send_returns_false_on_transport_error(respx_mock):
    """A network error during _post is logged and swallowed — the
    scheduler tick mustn't crash because Pushover is unreachable."""
    import httpx
    from app.services import pushover

    pushover._enabled = True
    pushover._app_token = "tok"
    pushover._user_key = "key"

    respx_mock.post(
        "https://api.pushover.net/1/messages.json"
    ).mock(side_effect=httpx.ConnectError("no route to host"))

    ok = await pushover.send("hello")
    assert ok is False


async def test_send_test_uses_credentials_regardless_of_enabled(respx_mock):
    """send_test ignores the `enabled` flag — used from the Settings
    page to verify creds before turning notifications on."""
    from app.services import pushover

    pushover._enabled = False  # disabled
    pushover._app_token = "tok"
    pushover._user_key = "key"

    route = respx_mock.post(
        "https://api.pushover.net/1/messages.json"
    ).respond(200, json={"status": 1})

    ok = await pushover.send_test()
    assert ok is True
    assert route.call_count == 1


# ── validate ────────────────────────────────────────────────────────────────


async def test_validate_calls_pushover_validate_endpoint(respx_mock):
    from app.services import pushover

    respx_mock.post(
        "https://api.pushover.net/1/users/validate.json"
    ).respond(200, json={"status": 1})

    ok, err = await pushover.validate("tok", "key")
    assert ok is True
    assert err == ""


async def test_validate_surfaces_pushover_error_strings(respx_mock):
    from app.services import pushover

    respx_mock.post(
        "https://api.pushover.net/1/users/validate.json"
    ).respond(200, json={"status": 0, "errors": ["application token is invalid"]})

    ok, err = await pushover.validate("tok", "key")
    assert ok is False
    assert "invalid" in err.lower()


async def test_validate_handles_transport_error():
    """No respx route configured → httpx fails → validate returns
    (False, error_string) instead of raising."""
    from app.services import pushover

    ok, err = await pushover.validate("tok", "key")
    assert ok is False
    assert err  # some error string


# ── Per-site send + config resolution ────────────────────────────────────────


async def test_send_with_site_id_resolves_per_site_config(
    db_session, respx_mock,
):
    """When site_id is passed, send() loads the site_settings row,
    decrypts the credentials, and uses them. The Main-only in-memory
    config is bypassed."""
    from app.models.site import Site, SiteSetting
    from app.services import pushover

    site = Site(name="X", slug="x", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    payload = {
        "app_token": pushover._encrypt("site-tok"),
        "user_key":  pushover._encrypt("site-key"),
        "enabled": True,
        "alert_sync_failure": True,
        "alert_instance_offline": True,
        "alert_high_block_rate": False,
        "alert_on_vip_transfer": False,
        "block_rate_threshold_pct": 50.0,
        "offline_alert_max_count": 1,
        "offline_alert_retries": 1,
    }
    db_session.add(SiteSetting(
        site_id=site.id, key="pushover_settings", value=json.dumps(payload),
    ))
    await db_session.commit()

    route = respx_mock.post(
        "https://api.pushover.net/1/messages.json"
    ).respond(200, json={"status": 1})

    ok = await pushover.send("hello", site_id=site.id)
    assert ok is True
    assert route.call_count == 1


async def test_send_with_site_id_no_config_returns_false(db_session):
    """A site without persisted Pushover config can't send — return
    False rather than falling back to the (potentially unrelated)
    Main config."""
    from app.models.site import Site
    from app.services import pushover

    site = Site(name="No config", slug="nopush", is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    ok = await pushover.send("body", site_id=site.id)
    assert ok is False


# ── Per-site notification helpers ────────────────────────────────────────────


async def test_notify_instance_offline_with_site_id_honours_per_site_toggle(
    db_session, respx_mock,
):
    """When site_id is passed and the site's alert_instance_offline
    is False, the helper short-circuits and never POSTs."""
    from app.models.site import Site, SiteSetting
    from app.services import pushover

    site = Site(name="Z", slug="z", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    payload = {
        "app_token": pushover._encrypt("t"),
        "user_key":  pushover._encrypt("k"),
        "enabled": True,
        "alert_sync_failure": False,
        "alert_instance_offline": False,  # ← gate
        "alert_high_block_rate": False,
        "alert_on_vip_transfer": False,
        "block_rate_threshold_pct": 50.0,
        "offline_alert_max_count": 1,
        "offline_alert_retries": 1,
    }
    db_session.add(SiteSetting(
        site_id=site.id, key="pushover_settings", value=json.dumps(payload),
    ))
    await db_session.commit()

    route = respx_mock.post(
        "https://api.pushover.net/1/messages.json"
    ).respond(200, json={"status": 1})

    await pushover.notify_instance_offline("p1", site_id=site.id)
    assert route.call_count == 0  # gated off


async def test_notify_vip_transfer_with_site_id_honours_per_site_toggle(
    db_session, respx_mock,
):
    from app.models.site import Site, SiteSetting
    from app.services import pushover

    site = Site(name="V", slug="v", is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    # Site config has alert_on_vip_transfer=False — must not fire.
    payload = {
        "app_token": pushover._encrypt("t"),
        "user_key":  pushover._encrypt("k"),
        "enabled": True,
        "alert_sync_failure": True,
        "alert_instance_offline": True,
        "alert_high_block_rate": False,
        "alert_on_vip_transfer": False,
        "block_rate_threshold_pct": 50.0,
        "offline_alert_max_count": 1,
        "offline_alert_retries": 1,
    }
    db_session.add(SiteSetting(
        site_id=site.id, key="pushover_settings", value=json.dumps(payload),
    ))
    await db_session.commit()

    route = respx_mock.post(
        "https://api.pushover.net/1/messages.json"
    ).respond(200, json={"status": 1})

    await pushover.notify_vip_transfer("master", "replica", site_id=site.id)
    assert route.call_count == 0
