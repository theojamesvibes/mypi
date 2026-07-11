"""Tests for /api/stats — summary, history, top."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _ensure_fernet_key():
    from cryptography.fernet import Fernet

    import app.models.pihole as pihole_models
    from app.config import settings

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None
    yield


@pytest.fixture
async def site(db_session):
    from app.models.site import Site
    s = Site(name="Main", slug="main", is_main=True, is_active=True, sort_order=0)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def instance(db_session, site):
    from app.models.pihole import PiholeInstance
    i = PiholeInstance(
        site_id=site.id, name="p1", url="http://p1",
        api_password="pw", is_active=True, is_master=True,
    )
    db_session.add(i)
    await db_session.commit()
    await db_session.refresh(i)
    return i


@pytest.fixture
async def seed_data(db_session, instance):
    """Some StatsSnapshot rows + QueryLog rows so the aggregate
    endpoints have something to chew on."""
    from app.models.pihole import QueryLog, StatsSnapshot

    now = datetime.now(UTC)
    db_session.add_all([
        StatsSnapshot(
            instance_id=instance.id,
            collected_at=now - timedelta(minutes=1),
            status="online",
            dns_queries_today=200, queries_blocked=50, percent_blocked=25.0,
            domains_on_blocklist=5000, unique_clients=10,
            queries_cached=80, queries_forwarded=70,
        ),
    ])
    for i in range(20):
        db_session.add(QueryLog(
            instance_id=instance.id,
            timestamp=now - timedelta(minutes=i),
            domain=f"ads{i % 3}.example",
            client_ip="10.0.0.5", client_name="laptop",
            status="GRAVITY" if i % 2 == 0 else "OK",
            query_type="A", reply_type="IP", reply_time_ms=1.5,
        ))
    await db_session.commit()
    return instance


# ── /api/stats/summary ───────────────────────────────────────────────────────


async def test_summary_unauth_returns_401(client):
    resp = await client.get("/api/stats/summary")
    assert resp.status_code == 401


async def test_summary_returns_aggregated_payload(authed_client, seed_data):
    resp = await authed_client.get("/api/stats/summary")
    assert resp.status_code == 200
    body = resp.json()
    # Aggregated shape — totals across all instances
    assert "totals" in body
    assert "instances" in body
    assert isinstance(body["instances"], list)


async def test_summary_per_site_scopes_to_site_instances(authed_client, seed_data):
    """The per-site variant filters to instances in that site only."""
    resp = await authed_client.get("/api/sites/main/stats/summary")
    assert resp.status_code == 200
    body = resp.json()
    # All returned instances belong to 'main'
    for inst in body.get("instances", []):
        assert inst.get("site_slug") == "main" or inst.get("site_slug") is None


# ── /api/stats/history ───────────────────────────────────────────────────────


async def test_history_returns_buckets(authed_client, seed_data):
    """24h history endpoint returns time-bucketed counts."""
    resp = await authed_client.get("/api/stats/history?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert "buckets" in body
    assert isinstance(body["buckets"], list)


async def test_history_unauth_returns_401(client):
    resp = await client.get("/api/stats/history")
    assert resp.status_code == 401


# ── /api/stats/top ───────────────────────────────────────────────────────────


async def test_top_returns_blocked_and_clients(authed_client, seed_data):
    """The /top endpoint yields top blocked domains + top clients."""
    resp = await authed_client.get("/api/stats/top?hours=24&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert "top_blocked" in body
    assert "top_clients" in body
    # We seeded 3 distinct ads*.example domains — at least one should appear.
    assert len(body["top_blocked"]) >= 1


async def test_top_per_site_returns_buckets(authed_client, seed_data):
    resp = await authed_client.get("/api/sites/main/stats/top?hours=24&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert "top_blocked" in body
    assert "top_clients" in body
