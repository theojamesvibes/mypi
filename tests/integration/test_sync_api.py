"""Tests for the /api/sync endpoints — status, schedule round-trip,
trigger validation."""
from __future__ import annotations

import pytest


@pytest.fixture
async def site(db_session):
    from app.models.site import Site
    s = Site(name="Main", slug="main", is_main=True, is_active=True, sort_order=0)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture(autouse=True)
def _reset_sync_state():
    from app.services import sync_service
    sync_service._state_by_site.clear()
    sync_service._lock_by_site.clear()
    sync_service._schedule_by_site.clear()
    yield


async def test_sync_status_default_is_idle(authed_client, site):
    resp = await authed_client.get("/api/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "idle"
    assert body["results"] == []


async def test_sync_schedule_round_trip(authed_client, site):
    """PUT then GET must return the same payload — settings persist
    via site_settings."""
    payload = {
        "interval_minutes": 30,
        "auto_gravity": True,
        "import_config": True,
        "import_gravity": True,
        "import_dhcp_leases": False,
        "run_gravity": True,
    }
    put = await authed_client.put("/api/sync/schedule", json=payload)
    assert put.status_code == 200

    got = await authed_client.get("/api/sync/schedule")
    assert got.status_code == 200
    body = got.json()
    assert body["interval_minutes"] == 30
    assert body["auto_gravity"] is True


async def test_sync_status_unauthenticated_returns_401(client):
    resp = await client.get("/api/sync/status")
    assert resp.status_code == 401


async def test_sync_schedule_put_blocked_on_readonly_key(
    client, readonly_api_key, site,
):
    """Read-only API key must NOT mutate the schedule."""
    resp = await client.put(
        "/api/sync/schedule",
        headers={"X-API-Key": readonly_api_key},
        json={
            "interval_minutes": 60, "auto_gravity": False,
            "import_config": True, "import_gravity": True,
            "import_dhcp_leases": False, "run_gravity": True,
        },
    )
    assert resp.status_code == 403


async def test_sync_trigger_when_already_running_returns_409(
    authed_client, site,
):
    from app.services import sync_service

    # Mark the site's sync as already in progress.
    state = sync_service._get_state_dict(site.id)
    state.status = "running"

    resp = await authed_client.post("/api/sync", json={
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    })
    assert resp.status_code == 409


async def test_sync_trigger_starts_background_sync(authed_client, site, monkeypatch):
    """Happy path: POST /api/sync returns "running" immediately and hands
    run_sync the exact toggles from the request body. run_sync itself is
    stubbed — the sync pipeline has its own suite."""
    from app.services import sync_service

    calls: list[dict] = []

    async def fake_run_sync(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(sync_service, "run_sync", fake_run_sync)

    resp = await authed_client.post("/api/sync", json={
        "import_config": True, "import_gravity": False,
        "import_dhcp_leases": True, "run_gravity": False,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"

    # ASGITransport awaits background tasks before returning the response,
    # so the stub has already run.
    assert calls == [{
        "import_config": True, "import_gravity": False,
        "import_dhcp_leases": True, "run_gravity": False,
    }]


async def test_sync_schedule_put_persist_failure_returns_500(
    authed_client, site, monkeypatch,
):
    from app.services import sync_service

    async def boom(**kwargs):
        raise RuntimeError("db went away")

    monkeypatch.setattr(sync_service, "set_schedule", boom)

    resp = await authed_client.put("/api/sync/schedule", json={
        "interval_minutes": 30, "auto_gravity": False,
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    })
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to save sync schedule."


# ── Per-site routes (/api/sites/{slug}/sync/...) ─────────────────────────────


@pytest.fixture
async def second_site(db_session):
    from app.models.site import Site
    s = Site(name="Cabin", slug="cabin", is_main=False, is_active=True, sort_order=1)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


async def test_site_sync_status_default_is_idle(authed_client, site):
    resp = await authed_client.get("/api/sites/main/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "idle"
    assert body["results"] == []


async def test_site_sync_status_unknown_slug_returns_404(authed_client, site):
    resp = await authed_client.get("/api/sites/nope/sync/status")
    assert resp.status_code == 404


async def test_site_sync_schedule_round_trip(authed_client, site, second_site):
    """Schedules are stored per site — writing Cabin's must not bleed
    into Main's."""
    put = await authed_client.put("/api/sites/cabin/sync/schedule", json={
        "interval_minutes": 45, "auto_gravity": True,
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    })
    assert put.status_code == 200

    got = await authed_client.get("/api/sites/cabin/sync/schedule")
    assert got.status_code == 200
    assert got.json()["interval_minutes"] == 45
    assert got.json()["auto_gravity"] is True

    main = await authed_client.get("/api/sites/main/sync/schedule")
    assert main.status_code == 200
    assert main.json()["interval_minutes"] == 0


async def test_site_sync_schedule_put_blocked_on_readonly_key(
    client, readonly_api_key, site,
):
    resp = await client.put(
        "/api/sites/main/sync/schedule",
        headers={"X-API-Key": readonly_api_key},
        json={
            "interval_minutes": 60, "auto_gravity": False,
            "import_config": True, "import_gravity": True,
            "import_dhcp_leases": False, "run_gravity": True,
        },
    )
    assert resp.status_code == 403


async def test_site_sync_trigger_passes_site_id(
    authed_client, site, second_site, monkeypatch,
):
    from app.services import sync_service

    calls: list[dict] = []

    async def fake_run_sync(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(sync_service, "run_sync", fake_run_sync)

    resp = await authed_client.post("/api/sites/cabin/sync", json={
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert len(calls) == 1
    assert calls[0]["site_id"] == second_site.id


async def test_site_sync_trigger_409_is_scoped_to_the_site(
    authed_client, site, second_site, monkeypatch,
):
    """Main mid-sync must not block a manual sync for Cabin — sync state
    is tracked per site."""
    from app.services import sync_service

    async def fake_run_sync(**kwargs):
        pass

    monkeypatch.setattr(sync_service, "run_sync", fake_run_sync)
    sync_service._get_state_dict(site.id).status = "running"

    blocked = await authed_client.post("/api/sites/main/sync", json={
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    })
    assert blocked.status_code == 409

    allowed = await authed_client.post("/api/sites/cabin/sync", json={
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    })
    assert allowed.status_code == 200


async def test_site_sync_trigger_blocked_on_readonly_key(
    client, readonly_api_key, site,
):
    resp = await client.post(
        "/api/sites/main/sync",
        headers={"X-API-Key": readonly_api_key},
        json={
            "import_config": True, "import_gravity": True,
            "import_dhcp_leases": False, "run_gravity": True,
        },
    )
    assert resp.status_code == 403
