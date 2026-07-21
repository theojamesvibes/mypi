"""Tests for the "Blocked by list" breakdown: /api/stats/blocked-by-list and
the pihole_lists sync job that feeds it."""
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
async def seed_blocks(db_session, instance):
    """Two adlists (one security), and gravity/regex/forwarded queries so the
    breakdown has a clear expected shape."""
    from app.models.pihole import PiholeList, QueryLog

    db_session.add_all([
        PiholeList(instance_id=instance.id, pihole_list_id=8, list_type="block",
                   address="https://raw.githubusercontent.com/hagezi/dns-blocklists/main/domains/tif.txt",
                   is_security=True),
        PiholeList(instance_id=instance.id, pihole_list_id=1, list_type="block",
                   address="https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
                   is_security=False),
    ])
    now = datetime.now(UTC)

    def qlog(status, list_id):
        return QueryLog(instance_id=instance.id, timestamp=now - timedelta(minutes=1),
                        domain="d.example", client_ip="10.0.0.5", status=status,
                        query_type="A", list_id=list_id)

    rows = [qlog("GRAVITY", 8) for _ in range(3)]        # security list
    rows += [qlog("GRAVITY", 1) for _ in range(2)]       # ad list
    rows.append(qlog("GRAVITY", 99))                     # unresolved -> Other
    rows.append(qlog("REGEX", 5))                        # not gravity -> excluded
    rows.append(qlog("FORWARDED", None))                 # not blocked -> excluded
    db_session.add_all(rows)
    await db_session.commit()
    return instance


async def test_blocked_by_list_unauth_returns_401(client):
    resp = await client.get("/api/stats/blocked-by-list")
    assert resp.status_code == 401


async def test_blocked_by_list_aggregates_and_flags_security(authed_client, seed_blocks):
    resp = await authed_client.get("/api/stats/blocked-by-list?hours=24")
    assert resp.status_code == 200
    lists = resp.json()["lists"]

    by_name = {e["name"]: e for e in lists}
    # gravity-only totals (regex + forwarded excluded)
    assert sum(e["count"] for e in lists) == 6
    # ordered by count desc — the security list leads
    assert [e["count"] for e in lists] == [3, 2, 1]

    tif = next(e for e in lists if "tif.txt" in e["name"])
    assert tif["count"] == 3 and tif["is_security"] is True
    sb = next(e for e in lists if "StevenBlack" in e["name"])
    assert sb["count"] == 2 and sb["is_security"] is False
    # the unresolved adlist id folds into one unclassified bucket
    assert by_name["Other / unclassified"]["count"] == 1


async def test_blocked_by_list_per_site_variant(authed_client, seed_blocks):
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
