"""More API + service coverage to push us above 80%.

Hits the lower-coverage paths in api/instances, api/sites,
api/stats history rollups, pushover load_settings + per-site sync
notifications, and api/_site_dep slug-history-410 path."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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


# ── api/instances stale list + per-site variants ────────────────────────────


@pytest.fixture
async def site_with_stale(db_session):
    from app.models.pihole import PiholeInstance
    from app.models.site import Site

    site = Site(name="Main", slug="main", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    db_session.add_all([
        PiholeInstance(
            site_id=site.id, name="active1", url="http://a1",
            api_password="p", is_active=True,
        ),
        PiholeInstance(
            site_id=site.id, name="orphan1", url="http://o1",
            api_password="p", is_active=False,
        ),
    ])
    await db_session.commit()
    return site


async def test_list_stale_instances(authed_client, site_with_stale):
    resp = await authed_client.get("/api/instances/stale")
    assert resp.status_code == 200
    body = resp.json()
    names = {i["name"] for i in body}
    assert "orphan1" in names
    assert "active1" not in names


async def test_list_instances_for_site(authed_client, site_with_stale):
    site = site_with_stale
    resp = await authed_client.get(f"/api/sites/{site.slug}/instances")
    assert resp.status_code == 200
    names = {i["name"] for i in resp.json()}
    assert "active1" in names
    assert "orphan1" not in names


async def test_list_stale_instances_for_site(authed_client, site_with_stale):
    site = site_with_stale
    resp = await authed_client.get(f"/api/sites/{site.slug}/instances/stale")
    assert resp.status_code == 200
    names = {i["name"] for i in resp.json()}
    assert "orphan1" in names


# ── api/sites slug history 410 ──────────────────────────────────────────────


async def test_delete_via_renamed_slug_returns_410(authed_client, db_session):
    """Hitting DELETE /api/sites/{old_slug} after a rename returns 410
    Gone with a hint that the slug was retired."""
    from app.models.site import Site, SiteSlugHistory

    site = Site(name="X", slug="new-x", is_active=False, sort_order=99)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    db_session.add(SiteSlugHistory(
        old_slug="old-x", site_id=site.id,
        retired_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    resp = await authed_client.delete("/api/sites/old-x")
    assert resp.status_code == 410


async def test_delete_unknown_site_returns_404(authed_client):
    resp = await authed_client.delete("/api/sites/never-existed")
    assert resp.status_code == 404


# ── api/stats history with seeded data ──────────────────────────────────────


@pytest.fixture
async def site_with_history(db_session):
    from app.models.pihole import PiholeInstance, QueryLog
    from app.models.site import Site

    site = Site(name="Main", slug="main-h", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    inst = PiholeInstance(
        site_id=site.id, name="p1", url="http://p1",
        api_password="p", is_active=True,
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)

    now = datetime.now(timezone.utc)
    rows = []
    for i in range(60):
        rows.append(QueryLog(
            instance_id=inst.id,
            timestamp=now - timedelta(minutes=i * 5),
            domain=f"d{i % 5}.example",
            client_ip=f"10.0.0.{(i % 4) + 1}",
            client_name="host",
            status="GRAVITY" if i % 3 == 0 else "OK",
            query_type="A", reply_type="IP", reply_time_ms=1.0,
        ))
    db_session.add_all(rows)
    await db_session.commit()
    return site


async def test_history_with_data_returns_buckets(authed_client, site_with_history):
    """history reports rolling-stats from StatsSnapshot rows, so the
    test fixture seeds QueryLog only — what matters is that the
    endpoint returns a well-formed shape, not specific values."""
    resp = await authed_client.get("/api/stats/history?hours=12")
    assert resp.status_code == 200
    body = resp.json()
    assert "buckets" in body
    assert isinstance(body["buckets"], list)


async def test_history_per_site_filters_to_site(authed_client, site_with_history):
    site = site_with_history
    resp = await authed_client.get(f"/api/sites/{site.slug}/stats/history?hours=12")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["buckets"]) > 0


async def test_top_per_site_with_data(authed_client, site_with_history):
    site = site_with_history
    resp = await authed_client.get(f"/api/sites/{site.slug}/stats/top?hours=12&limit=3")
    body = resp.json()
    assert len(body["top_blocked"]) >= 1


# ── pushover load_settings ───────────────────────────────────────────────────


async def test_pushover_load_settings_restores_module_state(db_session, monkeypatch):
    """load_settings reads from site_settings under Main and copies
    every field into module globals."""
    from app.models.site import Site, SiteSetting
    from app.services import pushover

    monkeypatch.setattr(pushover, "_app_token", "")
    monkeypatch.setattr(pushover, "_user_key", "")
    monkeypatch.setattr(pushover, "_enabled", False)

    site = Site(name="M", slug="m", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    payload = {
        "app_token": pushover._encrypt("loaded-tok"),
        "user_key":  pushover._encrypt("loaded-key"),
        "enabled": True,
        "alert_sync_failure": True,
        "alert_instance_offline": True,
        "alert_high_block_rate": False,
        "alert_on_vip_transfer": True,
        "block_rate_threshold_pct": 60.0,
        "offline_alert_max_count": 3,
        "offline_alert_retries": 2,
    }
    db_session.add(SiteSetting(
        site_id=site.id, key="pushover_settings", value=json.dumps(payload),
    ))
    await db_session.commit()

    await pushover.load_settings()

    assert pushover._app_token == "loaded-tok"
    assert pushover._user_key == "loaded-key"
    assert pushover._enabled is True
    assert pushover._offline_alert_retries == 2
    assert pushover._block_rate_threshold_pct == 60.0


# ── pushover notify_sync_failure with site_id ────────────────────────────────


async def test_notify_sync_failure_with_site_id_honours_per_site_toggle(
    db_session, respx_mock,
):
    """alert_sync_failure=False per-site → no Pushover call."""
    from app.models.site import Site, SiteSetting
    from app.services import pushover

    site = Site(name="X", slug="xx", is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    payload = {
        "app_token": pushover._encrypt("t"),
        "user_key":  pushover._encrypt("k"),
        "enabled": True,
        "alert_sync_failure": False,  # ← gate
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

    await pushover.notify_sync_failure("error", site_name="X", site_id=site.id)
    assert route.call_count == 0


async def test_notify_instance_back_online_with_site_id(
    db_session, respx_mock,
):
    from app.models.site import Site, SiteSetting
    from app.services import pushover

    site = Site(name="Y", slug="yy", is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

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

    await pushover.notify_instance_back_online("p1", site_id=site.id)
    assert route.call_count == 1
