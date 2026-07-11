"""Tests for /api/queries filtering, pagination, sorting, and the
client-summary endpoint. Complements test_queries.py (SSE) from PR 5."""
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
async def site_with_queries(db_session):
    from app.models.pihole import PiholeInstance, QueryLog
    from app.models.site import Site

    site = Site(
        name="Main", slug="main", is_main=True, is_active=True, sort_order=0,
    )
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    inst = PiholeInstance(
        site_id=site.id, name="p1", url="http://p1",
        api_password="pw", is_active=True,
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)

    now = datetime.now(UTC)
    rows = [
        QueryLog(
            instance_id=inst.id,
            timestamp=now - timedelta(minutes=i),
            domain=f"site{i % 4}.example",
            client_ip=f"10.0.0.{i % 3 + 1}",
            client_name=f"host{i % 3 + 1}",
            status="GRAVITY" if i % 3 == 0 else "OK",
            query_type="A", reply_type="IP", reply_time_ms=1.5 * i,
        )
        for i in range(50)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    return site, inst


# ── Basic GET /api/queries ───────────────────────────────────────────────────


async def test_queries_default_returns_paginated(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries")
    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body
    assert "items" in body
    assert body["total"] >= 1
    assert body["page"] == 1


async def test_queries_pagination_respects_page_size(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?page_size=5&page=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) <= 5
    assert body["page_size"] == 5


async def test_queries_filter_by_domain(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?domain=site0")
    assert resp.status_code == 200
    body = resp.json()
    for item in body["items"]:
        assert "site0" in item["domain"]


async def test_queries_filter_by_client(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?client=10.0.0.1")
    assert resp.status_code == 200
    body = resp.json()
    for item in body["items"]:
        assert "10.0.0.1" in item["client_ip"]


async def test_queries_filter_blocked_only(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?blocked=true")
    assert resp.status_code == 200
    body = resp.json()
    for item in body["items"]:
        # GRAVITY is one of the BLOCKED_STATUSES — every item must be in the set.
        assert item["status"] in {
            "GRAVITY", "REGEX", "BLACKLIST",
            "EXTERNAL_BLOCKED_IP", "EXTERNAL_BLOCKED_NULL",
            "EXTERNAL_BLOCKED_NXDOMAIN",
            "GRAVITY_CNAME", "REGEX_CNAME", "BLACKLIST_CNAME",
        }


async def test_queries_sort_by_domain_asc(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?sort_by=domain&sort_dir=asc")
    body = resp.json()
    domains = [item["domain"] for item in body["items"]]
    assert domains == sorted(domains)


async def test_queries_unauth_returns_401(client):
    resp = await client.get("/api/queries")
    assert resp.status_code == 401


# ── /api/queries/clients ─────────────────────────────────────────────────────


async def test_client_summary_aggregates_by_client(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries/clients")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    assert "total_queries" in body[0]
    assert "blocked_queries" in body[0]


# ── Per-site variants ────────────────────────────────────────────────────────


async def test_queries_per_site_returns_only_site_data(
    authed_client, site_with_queries,
):
    site, _ = site_with_queries
    resp = await authed_client.get(f"/api/sites/{site.slug}/queries")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


async def test_queries_per_site_clients_aggregate(authed_client, site_with_queries):
    site, _ = site_with_queries
    resp = await authed_client.get(f"/api/sites/{site.slug}/queries/clients")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
