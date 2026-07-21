"""Tests for the "Blocked by list" breakdown: /api/stats/blocked-by-list and
the pihole_lists sync job that feeds it."""
from __future__ import annotations

import contextlib
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


@pytest.fixture(autouse=True)
async def _reset_client_and_cache():
    """The breakdown now attributes via the master's live /api/search, so isolate
    the cached Pi-hole client and the per-scope attribution cache between tests."""
    import app.api.stats as stats_mod
    from app.services import client_manager

    client_manager._clients.clear()
    client_manager._last_persisted_sid.clear()
    stats_mod._bbl_cache.clear()
    yield
    for key in list(client_manager._clients):
        with contextlib.suppress(Exception):
            await client_manager._clients[key].close()
    client_manager._clients.clear()
    client_manager._last_persisted_sid.clear()
    stats_mod._bbl_cache.clear()


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


_TIF = "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/domains/tif.txt"
_SB = "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"


def _search_body(address=None, list_id=None):
    """Shape of GET /api/search — one matching block adlist, or none."""
    gravity = (
        []
        if address is None
        else [{"domain": "x", "type": "block", "address": address, "id": list_id, "enabled": True}]
    )
    return {"search": {"gravity": gravity, "domains": []}}


@pytest.fixture
async def seed_blocks(db_session, instance):
    """Two mirrored adlists (one security) + gravity/regex/forwarded queries over
    three domains, so live-search attribution has a clear expected shape."""
    from app.models.pihole import PiholeList, QueryLog

    db_session.add_all([
        PiholeList(instance_id=instance.id, pihole_list_id=8, list_type="block",
                   address=_TIF, is_security=True),
        PiholeList(instance_id=instance.id, pihole_list_id=1, list_type="block",
                   address=_SB, is_security=False),
    ])
    now = datetime.now(UTC)

    def qlog(domain, status):
        return QueryLog(instance_id=instance.id, timestamp=now - timedelta(minutes=1),
                        domain=domain, client_ip="10.0.0.5", status=status,
                        query_type="A", list_id=None)

    rows = [qlog("sec.example", "GRAVITY") for _ in range(3)]   # → tif (security)
    rows += [qlog("ad.example", "GRAVITY") for _ in range(2)]   # → StevenBlack
    rows.append(qlog("gone.example", "GRAVITY"))                # → no adlist → Other
    rows.append(qlog("re.example", "REGEX"))                    # not gravity → excluded
    rows.append(qlog("ok.example", "FORWARDED"))               # not blocked → excluded
    db_session.add_all(rows)
    await db_session.commit()
    return instance


def _mock_searches(respx_mock, inst):
    respx_mock.post(f"{inst.url}/api/auth").respond(200, json={"session": {"sid": "s"}})
    respx_mock.get(f"{inst.url}/api/search/sec.example").respond(200, json=_search_body(_TIF, 8))
    respx_mock.get(f"{inst.url}/api/search/ad.example").respond(200, json=_search_body(_SB, 1))
    respx_mock.get(f"{inst.url}/api/search/gone.example").respond(200, json=_search_body())


async def test_blocked_by_list_unauth_returns_401(client):
    resp = await client.get("/api/stats/blocked-by-list")
    assert resp.status_code == 401


async def test_blocked_by_list_aggregates_and_flags_security(
    authed_client, seed_blocks, respx_mock,
):
    """Attributes the busiest blocked domains to their adlist via live /api/search,
    sums by list, names from the address, and flags security feeds."""
    _mock_searches(respx_mock, seed_blocks)

    resp = await authed_client.get("/api/stats/blocked-by-list?hours=24")
    assert resp.status_code == 200
    lists = resp.json()["lists"]

    by_name = {e["name"]: e for e in lists}
    assert sum(e["count"] for e in lists) == 6
    # ordered by count desc — the security list leads
    assert [e["count"] for e in lists] == [3, 2, 1]

    tif = next(e for e in lists if "tif.txt" in e["name"])
    assert tif["count"] == 3 and tif["is_security"] is True
    sb = next(e for e in lists if "StevenBlack" in e["name"])
    assert sb["count"] == 2 and sb["is_security"] is False
    # a domain on no adlist folds into one unclassified bucket
    assert by_name["Other / unclassified"]["count"] == 1


async def test_blocked_by_list_per_site_variant(authed_client, seed_blocks, respx_mock):
    _mock_searches(respx_mock, seed_blocks)
    resp = await authed_client.get("/api/sites/main/stats/blocked-by-list")
    assert resp.status_code == 200
    assert sum(e["count"] for e in resp.json()["lists"]) == 6


async def test_sync_lists_computes_is_security_from_group(db_session, instance, monkeypatch):
    """_sync_lists_for stores block adlists, flags those in the configured
    security group, and skips allow-type lists."""
    from sqlalchemy import select

    import app.services.collector.lists as lists_mod
    from app.config import settings
    from app.models.pihole import PiholeList

    class FakeClient:
        sid = "s"
        async def get_groups(self):
            return {0: "Default", 7: "Security"}   # matched case-insensitively

        async def get_lists(self):
            return [
                {"id": 8, "type": "block", "address": "tif", "enabled": True, "groups": [7]},
                {"id": 1, "type": "block", "address": "sb", "enabled": True, "groups": [0]},
                {"id": 2, "type": "allow", "address": "allow", "enabled": True, "groups": [0]},
            ]

    async def fake_get_client(_inst):
        return FakeClient()

    monkeypatch.setattr(lists_mod, "get_client", fake_get_client)
    monkeypatch.setattr(lists_mod, "save_sid", lambda *a, **k: _noop())
    monkeypatch.setattr(settings, "security_group_name", "security")

    n = await lists_mod._sync_lists_for(instance)
    assert n == 2  # two block lists; the allow list is skipped

    rows = (await db_session.execute(
        select(PiholeList).where(PiholeList.instance_id == instance.id)
    )).scalars().all()
    assert {r.pihole_list_id: r.is_security for r in rows} == {8: True, 1: False}


async def _noop():
    return None
