"""Tests for /api/domains — block/unblock with master→replica fan-out
mocked via respx."""
from __future__ import annotations

import contextlib

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


@pytest.fixture(autouse=True)
async def _clear_client_registry():
    from app.services import client_manager
    client_manager._clients.clear()
    client_manager._last_persisted_sid.clear()
    yield
    for key in list(client_manager._clients):
        with contextlib.suppress(Exception):
            await client_manager._clients[key].close()
    client_manager._clients.clear()
    client_manager._last_persisted_sid.clear()


@pytest.fixture
async def cluster(db_session):
    from app.models.pihole import PiholeInstance
    from app.models.site import Site

    site = Site(
        name="Main", slug="main", is_main=True, is_active=True, sort_order=0,
    )
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    master = PiholeInstance(
        site_id=site.id, name="master", url="http://master.test",
        api_password="pw", is_active=True, is_master=True,
    )
    replica = PiholeInstance(
        site_id=site.id, name="replica", url="http://replica.test",
        api_password="pw", is_active=True, is_master=False,
    )
    db_session.add_all([master, replica])
    await db_session.commit()
    await db_session.refresh(master)
    await db_session.refresh(replica)
    return site, master, replica


# ── Domain status ────────────────────────────────────────────────────────────


async def test_get_domain_status_unauth_returns_401(client):
    resp = await client.get("/api/domains/status/example.com")
    assert resp.status_code == 401


async def test_get_domain_status_returns_lookup_result(
    authed_client, cluster, respx_mock,
):
    """GET /api/domains/status/{domain} hits the master's deny+allow
    list lookups in parallel."""
    _, master, _ = cluster
    respx_mock.post(f"{master.url}/api/auth").respond(
        200, json={"session": {"sid": "x"}}
    )
    respx_mock.get(f"{master.url}/api/domains/deny/exact").respond(
        200, json={"domains": []}
    )
    respx_mock.get(f"{master.url}/api/domains/allow/exact").respond(
        200, json={"domains": []}
    )

    resp = await authed_client.get("/api/domains/status/example.com")
    assert resp.status_code == 200
    body = resp.json()
    assert "in_deny" in body
    assert "in_allow" in body
    assert body["in_deny"] is False
    assert body["in_allow"] is False


# ── Block + unblock ──────────────────────────────────────────────────────────


async def test_add_to_deny_returns_per_instance_results(
    authed_client, cluster, respx_mock,
):
    """POST /api/domains/deny first removes the domain from the allow
    list (so it can't override) then adds it to deny — across master
    + replica."""
    _, master, replica = cluster

    for url in (master.url, replica.url):
        respx_mock.post(f"{url}/api/auth").respond(
            200, json={"session": {"sid": "s"}}
        )
        # allow → deny means a DELETE on allow first, then POST on deny.
        respx_mock.delete(f"{url}/api/domains/allow/exact/ads.example").respond(204)
        respx_mock.post(f"{url}/api/domains/deny/exact").respond(200)

    resp = await authed_client.post(
        "/api/domains/deny", json={"domain": "ads.example"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert len(body["results"]) == 2


async def test_remove_from_deny_returns_per_instance_results(
    authed_client, cluster, respx_mock,
):
    _, master, replica = cluster
    for url in (master.url, replica.url):
        respx_mock.post(f"{url}/api/auth").respond(
            200, json={"session": {"sid": "s"}}
        )
        respx_mock.delete(f"{url}/api/domains/deny/exact/ads.example").respond(204)

    resp = await authed_client.delete("/api/domains/deny/ads.example")
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 2


async def test_add_to_allow_works_across_cluster(
    authed_client, cluster, respx_mock,
):
    _, master, replica = cluster
    for url in (master.url, replica.url):
        respx_mock.post(f"{url}/api/auth").respond(
            200, json={"session": {"sid": "s"}}
        )
        # add_to_allow does DELETE on deny first, then POST on allow.
        respx_mock.delete(f"{url}/api/domains/deny/exact/needed.example").respond(204)
        respx_mock.post(f"{url}/api/domains/allow/exact").respond(200)

    resp = await authed_client.post(
        "/api/domains/allow", json={"domain": "needed.example"},
    )
    assert resp.status_code == 200


async def test_block_endpoint_blocked_for_readonly_key(
    client, readonly_api_key, cluster,
):
    """Domain block is a mutation — read-only API keys must 403."""
    resp = await client.post(
        "/api/domains/deny",
        headers={"X-API-Key": readonly_api_key},
        json={"domain": "ads.example"},
    )
    assert resp.status_code == 403


# ── Per-site scoping ─────────────────────────────────────────────────────────


async def test_get_domain_status_for_site_scopes_to_master(
    authed_client, cluster, respx_mock,
):
    """The per-site status route resolves the site and reads its master."""
    _, master, _ = cluster
    respx_mock.post(f"{master.url}/api/auth").respond(200, json={"session": {"sid": "x"}})
    respx_mock.get(f"{master.url}/api/domains/deny/exact").respond(200, json={"domains": []})
    respx_mock.get(f"{master.url}/api/domains/allow/exact").respond(200, json={"domains": []})

    resp = await authed_client.get("/api/sites/main/domains/status/example.com")
    assert resp.status_code == 200
    assert resp.json()["effective"] == "unmanaged"


async def test_add_to_deny_for_site_returns_scope_note(
    authed_client, cluster, respx_mock,
):
    """A site-scoped mutation reports the site it changed and (for a single-site
    deployment) an empty other_sites reminder list."""
    _, master, replica = cluster
    for url in (master.url, replica.url):
        respx_mock.post(f"{url}/api/auth").respond(200, json={"session": {"sid": "s"}})
        respx_mock.delete(f"{url}/api/domains/allow/exact/ads.example").respond(204)
        respx_mock.post(f"{url}/api/domains/deny/exact").respond(200)

    resp = await authed_client.post(
        "/api/sites/main/domains/deny", json={"domain": "ads.example"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["site_name"] == "Main"
    assert body["other_sites"] == []
    assert len(body["results"]) == 2


# ── Which-list drill-down (live /api/search) ─────────────────────────────────


async def test_domain_blocklists_unauth_returns_401(client):
    resp = await client.get("/api/domains/blocklists/ads.example")
    assert resp.status_code == 401


async def test_domain_blocklists_returns_matching_adlist(
    authed_client, cluster, respx_mock,
):
    """The drill-down resolves which adlist blocks a domain via the master's
    live /api/search, naming the list from its address."""
    _, master, _ = cluster
    respx_mock.post(f"{master.url}/api/auth").respond(200, json={"session": {"sid": "x"}})
    respx_mock.get(f"{master.url}/api/search/ads.example").respond(
        200,
        json={
            "search": {
                "gravity": [
                    {
                        "domain": "ads.example",
                        "type": "block",
                        "address": "https://lists.example/hosts.txt",
                        "id": 1,
                        "enabled": True,
                    }
                ],
                "domains": [],
            }
        },
    )

    resp = await authed_client.get("/api/domains/blocklists/ads.example")
    assert resp.status_code == 200
    body = resp.json()
    assert body["domain"] == "ads.example"
    assert len(body["lists"]) == 1
    entry = body["lists"][0]
    assert entry["kind"] == "gravity"
    assert entry["address"] == "https://lists.example/hosts.txt"
    assert "hosts.txt" in entry["name"]
