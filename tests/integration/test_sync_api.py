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
