"""Tests for /api/queries filtering, pagination, sorting, and the
client-summary endpoint. Complements test_queries.py (SSE) from PR 5.

Seeded-data contract (see `site_with_queries`): 50 rows, i in 0..49 —
  domain    = site{i%4}.example   → site0×13, site1×13, site2×12, site3×12
  client_ip = 10.0.0.{i%3+1}      → .1×17, .2×17, .3×16 (names host1..3)
  status    = GRAVITY if i%3==0   → 17 blocked (all on 10.0.0.1), 33 "OK"
The filter tests assert exact totals against those counts so a filter
that silently stops filtering (or filters everything away) fails loudly
instead of passing vacuously.
"""
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


@pytest.fixture
async def other_site_with_queries(db_session, site_with_queries):
    """A second (non-main) site with its own instance and 10 query rows
    on a distinct domain/client. Used by the per-site tests to prove
    site scoping actually *excludes* foreign rows — without this seed a
    broken site filter would still return a plausible-looking payload."""
    from app.models.pihole import PiholeInstance, QueryLog
    from app.models.site import Site

    site = Site(
        name="Branch", slug="branch", is_main=False, is_active=True, sort_order=1,
    )
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    inst = PiholeInstance(
        site_id=site.id, name="p2", url="http://p2",
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
            domain="othersite.example",
            client_ip="10.9.9.9",
            client_name="otherhost",
            status="OK",
            query_type="A", reply_type="IP", reply_time_ms=2.0,
        )
        for i in range(10)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    return site, inst


# ── Basic GET /api/queries ───────────────────────────────────────────────────


async def test_queries_default_returns_paginated(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries")
    assert resp.status_code == 200
    body = resp.json()
    # Exactly the 50 seeded rows — all within the default 24h window,
    # all on the first page (default page_size=100).
    assert body["total"] == 50
    assert len(body["items"]) == 50
    assert body["page"] == 1


async def test_queries_pagination_respects_page_size(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?page_size=5&page=1")
    assert resp.status_code == 200
    body = resp.json()
    # Exactly 5 (not "<= 5", which an empty result would satisfy) and
    # total still reports the full unpaginated count.
    assert len(body["items"]) == 5
    assert body["page_size"] == 5
    assert body["total"] == 50

    # Page 2 returns the *next* rows, not the same ones again.
    resp2 = await authed_client.get("/api/queries?page_size=5&page=2")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["items"]) == 5
    page1_ids = {item["id"] for item in body["items"]}
    page2_ids = {item["id"] for item in body2["items"]}
    assert page1_ids.isdisjoint(page2_ids)


async def test_queries_filter_by_domain(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?domain=site0")
    assert resp.status_code == 200
    body = resp.json()
    # Exactly the 13 site0.example rows: the exact-count assertion is
    # what proves rows for site1/2/3 were excluded (a no-op filter
    # would return 50) and that matching rows weren't dropped (an
    # over-eager filter would return 0, passing a per-item-only check).
    assert body["total"] == 13
    assert len(body["items"]) == 13
    assert all(item["domain"] == "site0.example" for item in body["items"])


async def test_queries_filter_by_client(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?client=10.0.0.1")
    assert resp.status_code == 200
    body = resp.json()
    # 17 rows belong to 10.0.0.1; rows for .2/.3 must be excluded and
    # none of the 17 matching rows dropped.
    assert body["total"] == 17
    assert len(body["items"]) == 17
    assert all(item["client_ip"] == "10.0.0.1" for item in body["items"])


async def test_queries_filter_blocked_only(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?blocked=true")
    assert resp.status_code == 200
    body = resp.json()
    # Seed has 17 GRAVITY rows (blocked) and 33 "OK" rows (permitted).
    # total==17 proves the OK rows were excluded *and* no blocked row
    # was lost — an ignored filter returns 50, a broken one returns 0.
    assert body["total"] == 17
    assert len(body["items"]) == 17
    assert all(item["status"] == "GRAVITY" for item in body["items"])

    # And the complement: blocked=false returns exactly the permitted rows.
    resp2 = await authed_client.get("/api/queries?blocked=false")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["total"] == 33
    assert all(item["status"] == "OK" for item in body2["items"])


async def test_queries_sort_by_domain_asc(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries?sort_by=domain&sort_dir=asc")
    body = resp.json()
    domains = [item["domain"] for item in body["items"]]
    # Guard against vacuity: an empty page (or a single distinct value)
    # is trivially "sorted". All 50 rows across 4 distinct domains must
    # come back, in order — default timestamp ordering would interleave
    # site0..site3, so this fails if sort_by is ignored.
    assert len(domains) == 50
    assert len(set(domains)) == 4
    assert domains == sorted(domains)


async def test_queries_unauth_returns_401(client):
    resp = await client.get("/api/queries")
    assert resp.status_code == 401


# ── /api/queries/clients ─────────────────────────────────────────────────────


async def test_client_summary_aggregates_by_client(authed_client, site_with_queries):
    resp = await authed_client.get("/api/queries/clients")
    assert resp.status_code == 200
    body = resp.json()
    # One row per unique client with exact aggregate counts. All 17
    # GRAVITY rows sit on 10.0.0.1 (i%3==0 drives both), so its
    # blocked count equals its total; the others must report 0.
    by_ip = {row["client_ip"]: row for row in body}
    assert set(by_ip) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}
    assert by_ip["10.0.0.1"]["total_queries"] == 17
    assert by_ip["10.0.0.1"]["blocked_queries"] == 17
    assert by_ip["10.0.0.2"]["total_queries"] == 17
    assert by_ip["10.0.0.2"]["blocked_queries"] == 0
    assert by_ip["10.0.0.3"]["total_queries"] == 16
    assert by_ip["10.0.0.3"]["blocked_queries"] == 0


# ── Per-site variants ────────────────────────────────────────────────────────


async def test_queries_per_site_returns_only_site_data(
    authed_client, site_with_queries, other_site_with_queries,
):
    site, _ = site_with_queries
    resp = await authed_client.get(f"/api/sites/{site.slug}/queries")
    assert resp.status_code == 200
    body = resp.json()
    # Only Main's 50 rows — the Branch site's 10 `othersite.example`
    # rows must be excluded (an unscoped query would return 60).
    assert body["total"] == 50
    domains = {item["domain"] for item in body["items"]}
    assert "othersite.example" not in domains
    assert domains == {"site0.example", "site1.example", "site2.example", "site3.example"}

    # The other site's endpoint conversely sees only its own rows.
    other_site, _ = other_site_with_queries
    resp2 = await authed_client.get(f"/api/sites/{other_site.slug}/queries")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["total"] == 10
    assert all(item["domain"] == "othersite.example" for item in body2["items"])


async def test_queries_per_site_clients_aggregate(
    authed_client, site_with_queries, other_site_with_queries,
):
    site, _ = site_with_queries
    resp = await authed_client.get(f"/api/sites/{site.slug}/queries/clients")
    assert resp.status_code == 200
    body = resp.json()
    # Exactly Main's three clients with exact counts — the Branch
    # site's 10.9.9.9 must not leak into the aggregate.
    by_ip = {row["client_ip"]: row for row in body}
    assert set(by_ip) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}
    assert by_ip["10.0.0.1"]["total_queries"] == 17
    assert by_ip["10.0.0.1"]["blocked_queries"] == 17
    assert by_ip["10.0.0.2"]["total_queries"] == 17
    assert by_ip["10.0.0.3"]["total_queries"] == 16
