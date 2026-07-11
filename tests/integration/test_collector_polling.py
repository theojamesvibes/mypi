"""Tests for collector.py polling, versioning, backfill and scheduler
registration paths — the hot path that runs every 60 s in prod.

The state-machine tests in tests/services/test_collector_state.py
exercise the per-tick decision logic. This file fills in the parts
that touch the DB and the Pi-hole HTTP surface — _poll_stats_for,
_poll_queries_for, fetch_all_instance_versions, backfill_queries_for,
and schedule_site / unschedule_site / reschedule_all_queries_jobs.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

PIHOLE = "http://pihole.test"


# ── State + pushover stubs ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_collector_state():
    from app.services import collector
    state_dicts = (
        collector._consec_failures, collector._cooldown_until,
        collector._last_failure_at, collector._last_seen_ts,
        collector._prev_status, collector._offline_retry_count,
        collector._offline_alert_count, collector._stall_count,
        collector._stall_alerted, collector._prev_dns_queries_today,
        collector._prev_watermark_for_stall,
        collector._vip_last_advance_seq, collector._vip_advance_streak,
        collector._vip_active_node, collector._vip_group_stall_alerted,
        collector._site_poll_seq,
    )
    for d in state_dicts:
        d.clear()
    yield
    for d in state_dicts:
        d.clear()


@pytest.fixture
def pushover_calls(monkeypatch):
    """Capture every notify_* call made via pushover_service. Returns
    a list of (function_name, args, kwargs) tuples so tests can assert
    which alerts fired."""
    from app.services import pushover

    calls: list = []

    def _make_stub(name: str):
        async def stub(*args, **kwargs):
            calls.append((name, args, kwargs))
        return stub

    for name in (
        "notify_instance_offline", "notify_instance_back_online",
        "notify_instance_stalled", "notify_instance_recovered_from_stall",
        "notify_vip_transfer", "notify_vip_group_stalled",
        "notify_vip_group_recovered", "notify_sync_failure",
    ):
        monkeypatch.setattr(pushover, name, _make_stub(name))
    return calls


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


# ── Site + instance fixtures ─────────────────────────────────────────────────


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
    from cryptography.fernet import Fernet

    import app.models.pihole as pihole_models
    from app.config import settings
    from app.models.pihole import PiholeInstance

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    inst = PiholeInstance(
        site_id=site.id, name="pihole-1", url=PIHOLE,
        api_password="pw", is_active=True,
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)
    return inst


# ── _poll_stats_for happy + failure paths ────────────────────────────────────


async def test_poll_stats_for_stores_snapshot_on_success(
    instance, respx_mock, pushover_calls,
):
    """Online poll: persists a `online` StatsSnapshot, advances
    instance.last_seen_at. No alert fires (no prior offline state)."""
    from app.database import AsyncSessionLocal
    from app.models.pihole import PiholeInstance, StatsSnapshot
    from app.services.collector import _poll_stats_for

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid-1"}}
    )
    respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(
        200, json={
            "queries": {"total": 100, "blocked": 20, "percent_blocked": 20.0,
                        "cached": 30, "forwarded": 50},
            "gravity": {"domains_being_blocked": 1000},
            "clients": {"active": 5},
        },
    )

    snapshot, advanced = await _poll_stats_for(instance, site_name="Main")

    assert snapshot.status == "online"
    assert snapshot.dns_queries_today == 100
    assert advanced is None  # bootstrap poll — no prior baseline

    async with AsyncSessionLocal() as fresh:
        snaps = (await fresh.execute(select(StatsSnapshot))).scalars().all()
        inst = (
            await fresh.execute(select(PiholeInstance).where(
                PiholeInstance.id == instance.id,
            ))
        ).scalar_one()

    assert len(snaps) == 1
    assert snaps[0].status == "online"
    assert inst.last_seen_at is not None
    assert pushover_calls == []


async def test_poll_stats_for_persists_offline_snapshot_on_connect_error(
    instance, respx_mock, pushover_calls,
):
    """Connection error during the auth step → status='offline'
    snapshot row, no last_seen_at advance."""
    from app.database import AsyncSessionLocal
    from app.models.pihole import StatsSnapshot
    from app.services.collector import _poll_stats_for

    respx_mock.post(f"{PIHOLE}/api/auth").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    snapshot, _ = await _poll_stats_for(instance, site_name="Main")
    assert snapshot.status == "offline"

    async with AsyncSessionLocal() as fresh:
        snaps = (await fresh.execute(select(StatsSnapshot))).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].status == "offline"


async def test_poll_stats_for_circuit_open_skips_http(
    instance, respx_mock, pushover_calls,
):
    """Pre-arm the breaker so _breaker_allows returns False — the
    poll must persist an `offline` snapshot WITHOUT making any
    /api/auth POST."""
    from app.services import collector
    from app.services.collector import _poll_stats_for

    auth_route = respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "x"}}
    )
    collector._cooldown_until[str(instance.id)] = (
        datetime.now(UTC) + timedelta(seconds=300)
    )

    snapshot, _ = await _poll_stats_for(instance, site_name="Main")
    assert snapshot.status == "offline"
    assert auth_route.call_count == 0


async def test_offline_alert_fires_after_retries_exhausted(
    instance, respx_mock, pushover_calls, monkeypatch,
):
    """First offline poll establishes baseline. Subsequent offline
    polls advance the retry counter; once `_offline_alert_retries`
    is exhausted, notify_instance_offline fires."""
    from app.services import pushover
    from app.services.collector import _poll_stats_for

    monkeypatch.setattr(pushover, "get_offline_alert_retries", lambda: 1)
    monkeypatch.setattr(pushover, "get_offline_alert_max_count", lambda: 1)

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid"}}
    )
    respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(
        200, json={
            "queries": {"total": 100, "blocked": 20, "percent_blocked": 20.0,
                        "cached": 30, "forwarded": 50},
            "gravity": {"domains_being_blocked": 1000},
            "clients": {"active": 5},
        },
    )
    # First poll succeeds, establishing baseline.
    await _poll_stats_for(instance, site_name="Main")
    assert pushover_calls == []

    # Now switch to error responses for subsequent polls.
    respx_mock.reset()
    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid"}}
    )
    respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(500)

    # Poll 2: online → offline transition. Retry counter starts.
    await _poll_stats_for(instance, site_name="Main")
    await asyncio.sleep(0.05)
    assert all(c[0] != "notify_instance_offline" for c in pushover_calls)

    # Poll 3: still offline. Retry counter advances (1/1).
    await _poll_stats_for(instance, site_name="Main")
    await asyncio.sleep(0.05)

    # Poll 4: retries exhausted, alert fires.
    await _poll_stats_for(instance, site_name="Main")
    await asyncio.sleep(0.05)
    assert any(c[0] == "notify_instance_offline" for c in pushover_calls)


# ── _poll_queries_for ────────────────────────────────────────────────────────


async def test_poll_queries_for_stores_new_rows(instance, respx_mock):
    """Fresh queries from /api/queries become QueryLog rows."""
    from app.database import AsyncSessionLocal
    from app.models.pihole import QueryLog
    from app.services.collector import _poll_queries_for

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid"}}
    )
    respx_mock.get(f"{PIHOLE}/api/queries").respond(
        200,
        json={
            "queries": [
                {"id": "q1", "time": 1700000000, "type": "A",
                 "domain": "ads.example", "client": {"ip": "10.0.0.1", "name": "host1"},
                 "status": "BLOCKED", "reply": {"type": "NODATA", "time": 0.0}},
                {"id": "q2", "time": 1700000001, "type": "A",
                 "domain": "ok.example", "client": {"ip": "10.0.0.1", "name": "host1"},
                 "status": "OK", "reply": {"type": "IP", "time": 5.0}},
            ],
        },
    )

    await _poll_queries_for(instance)

    async with AsyncSessionLocal() as fresh:
        logs = (await fresh.execute(select(QueryLog))).scalars().all()
    assert len(logs) == 2
    assert {log.domain for log in logs} == {"ads.example", "ok.example"}


async def test_poll_queries_for_dedupes_by_pihole_query_id(instance, respx_mock):
    """Re-fetching the same query (same pihole_id) must not insert
    a duplicate row — idempotent storage."""
    from app.database import AsyncSessionLocal
    from app.models.pihole import QueryLog
    from app.services.collector import _poll_queries_for

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid"}}
    )
    payload = {
        "queries": [
            {"id": "q1", "time": 1700000000, "type": "A",
             "domain": "ads.example", "client": {"ip": "10.0.0.1", "name": "h"},
             "status": "BLOCKED", "reply": {"type": "NODATA", "time": 0.0}},
        ],
    }
    respx_mock.get(f"{PIHOLE}/api/queries").respond(200, json=payload)

    await _poll_queries_for(instance)
    await _poll_queries_for(instance)

    async with AsyncSessionLocal() as fresh:
        logs = (await fresh.execute(select(QueryLog))).scalars().all()
    assert len(logs) == 1


async def test_poll_queries_for_advances_watermark(instance, respx_mock):
    """`_last_seen_ts[instance_id]` advances to the most recent
    query timestamp so the next poll passes `from=`."""
    from app.services import collector
    from app.services.collector import _poll_queries_for

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid"}}
    )
    respx_mock.get(f"{PIHOLE}/api/queries").respond(
        200,
        json={
            "queries": [
                {"id": "q1", "time": 1700000050, "type": "A",
                 "domain": "x", "client": {"ip": "1", "name": "h"},
                 "status": "OK", "reply": {"type": "IP", "time": 0.0}},
            ],
        },
    )
    await _poll_queries_for(instance)
    assert collector._last_seen_ts[str(instance.id)] == 1700000050.0


# ── poll_stats_for_site / poll_queries_for_site ──────────────────────────────


async def test_poll_stats_for_site_skips_when_no_active_instances(
    site, respx_mock,
):
    """An empty (but existing) site is a fast no-op — must not
    explode trying to gather() over zero instances."""
    from app.services.collector import poll_stats_for_site
    # Site exists but has no instances. Must complete cleanly.
    await poll_stats_for_site(site.id)


async def test_poll_stats_for_site_runs_per_instance(
    instance, respx_mock, pushover_calls,
):
    """Verifies the per-site fan-out actually polls each instance."""
    from app.database import AsyncSessionLocal
    from app.models.pihole import StatsSnapshot
    from app.services.collector import poll_stats_for_site

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid"}}
    )
    respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(
        200,
        json={
            "queries": {"total": 50, "blocked": 5, "percent_blocked": 10.0,
                        "cached": 10, "forwarded": 35},
            "gravity": {"domains_being_blocked": 1000},
            "clients": {"active": 3},
        },
    )

    await poll_stats_for_site(instance.site_id)

    async with AsyncSessionLocal() as fresh:
        snaps = (await fresh.execute(select(StatsSnapshot))).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].status == "online"


# ── Versioning ───────────────────────────────────────────────────────────────


async def test_fetch_version_for_persists_version_columns(
    instance, respx_mock,
):
    """version_core / version_ftl / version_web get filled in on the
    instance row from the /api/info/version response."""
    from app.database import AsyncSessionLocal
    from app.models.pihole import PiholeInstance
    from app.services.collector import _fetch_version_for

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid"}}
    )
    respx_mock.get(f"{PIHOLE}/api/info/version").respond(
        200,
        json={"version": {
            "core": {"local": {"version": "v6.4.2"},
                     "remote": {"version": "v6.4.2"},
                     "update_available": False},
            "ftl":  {"local": {"version": "v6.6.1"},
                     "remote": {"version": "v6.6.1"},
                     "update_available": False},
            "web":  {"local": {"version": "v6.5"},
                     "remote": {"version": "v6.5"},
                     "update_available": False},
        }},
    )

    await _fetch_version_for(instance)

    async with AsyncSessionLocal() as fresh:
        inst = (
            await fresh.execute(select(PiholeInstance).where(
                PiholeInstance.id == instance.id,
            ))
        ).scalar_one()
    assert inst.version_core == "6.4.2"
    assert inst.version_ftl == "6.6.1"
    assert inst.version_web == "6.5"


async def test_fetch_all_instance_versions_iterates_active(
    instance, db_session, respx_mock,
):
    from cryptography.fernet import Fernet

    import app.models.pihole as pihole_models
    from app.config import settings
    from app.database import AsyncSessionLocal
    from app.models.pihole import PiholeInstance
    from app.services.collector import fetch_all_instance_versions

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    # Add a second active instance + one inactive that must be skipped.
    second_url = "http://pihole2.test"
    second = PiholeInstance(
        site_id=instance.site_id, name="pihole-2", url=second_url,
        api_password="pw", is_active=True,
    )
    inactive = PiholeInstance(
        site_id=instance.site_id, name="orphan", url="http://orphan.test",
        api_password="pw", is_active=False,
    )
    db_session.add_all([second, inactive])
    await db_session.commit()

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "s1"}}
    )
    respx_mock.post(f"{second_url}/api/auth").respond(
        200, json={"session": {"sid": "s2"}}
    )
    version_payload = {"version": {
        "core": {"local": {"version": "v6.4.2"}, "remote": {"version": "v6.4.2"},
                 "update_available": False},
        "ftl":  {"local": {"version": "v6.6.1"}, "remote": {"version": "v6.6.1"},
                 "update_available": False},
        "web":  {"local": {"version": "v6.5"}, "remote": {"version": "v6.5"},
                 "update_available": False},
    }}
    respx_mock.get(f"{PIHOLE}/api/info/version").respond(200, json=version_payload)
    respx_mock.get(f"{second_url}/api/info/version").respond(200, json=version_payload)
    # Inactive instance has NO route configured — if the code tried to
    # hit it the test would fail with respx routing assertion.

    await fetch_all_instance_versions()

    async with AsyncSessionLocal() as fresh:
        actives = (
            await fresh.execute(
                select(PiholeInstance).where(PiholeInstance.is_active.is_(True))
            )
        ).scalars().all()
    for inst in actives:
        assert inst.version_core == "6.4.2"


# ── Backfill ─────────────────────────────────────────────────────────────────


async def test_backfill_skips_when_recent_data_exists(
    instance, db_session, respx_mock,
):
    """If the most recent stored query is within the recency window,
    backfill is a no-op — no /api/auth, no /api/queries calls."""
    from app.models.pihole import QueryLog
    from app.services.collector import backfill_queries_for

    db_session.add(QueryLog(
        instance_id=instance.id,
        timestamp=datetime.now(UTC) - timedelta(seconds=30),
        domain="recent.example", status="OK",
    ))
    await db_session.commit()

    auth_route = respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "x"}}
    )
    queries_route = respx_mock.get(f"{PIHOLE}/api/queries").respond(
        200, json={"queries": []}
    )

    await backfill_queries_for(instance)

    assert auth_route.call_count == 0
    assert queries_route.call_count == 0


async def test_backfill_fetches_from_last_stored_timestamp(
    instance, db_session, respx_mock,
):
    """Stored data is older than the recency window — backfill
    fetches every clock-aligned hour from the last stored timestamp
    forward."""
    from app.models.pihole import QueryLog
    from app.services.collector import backfill_queries_for

    old_ts = datetime.now(UTC) - timedelta(hours=2, minutes=30)
    db_session.add(QueryLog(
        instance_id=instance.id, timestamp=old_ts,
        domain="old.example", status="OK",
    ))
    await db_session.commit()

    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "x"}}
    )
    queries_route = respx_mock.get(f"{PIHOLE}/api/queries").respond(
        200, json={"queries": []}
    )

    await backfill_queries_for(instance)

    # At least one window — the gap from old_ts to now is ~2.5h, so
    # 3 clock-aligned hourly windows.
    assert queries_route.call_count >= 1


# ── Scheduler registration ───────────────────────────────────────────────────


async def test_schedule_site_registers_two_jobs(site):
    """A site gets a `poll_stats_site_<id>` and a `poll_queries_site_<id>`
    APScheduler job pair."""
    from app.services.collector import _site_job_id, schedule_site

    sched = AsyncIOScheduler()
    schedule_site(sched, site.id, stats_interval_seconds=60, queries_interval_seconds=10)
    job_ids = {job.id for job in sched.get_jobs()}
    assert _site_job_id(site.id, "stats") in job_ids
    assert _site_job_id(site.id, "queries") in job_ids
    assert len(job_ids) == 2


async def test_unschedule_site_removes_both_jobs(site):
    from app.services.collector import schedule_site, unschedule_site

    sched = AsyncIOScheduler()
    schedule_site(sched, site.id, stats_interval_seconds=60, queries_interval_seconds=10)
    assert len(sched.get_jobs()) == 2

    unschedule_site(sched, site.id)
    assert len(sched.get_jobs()) == 0


async def test_reschedule_all_queries_jobs_only_touches_queries(site):
    """Changing the global queries-poll interval reschedules the
    queries job for every registered site without disturbing the
    stats jobs."""
    from app.services.collector import (
        reschedule_all_queries_jobs,
        schedule_site,
    )

    sched = AsyncIOScheduler()
    schedule_site(sched, site.id, stats_interval_seconds=60, queries_interval_seconds=10)

    # Capture the interval *value* (not the job ref) — APScheduler mutates
    # the same job object on reschedule, so a reference would update under us.
    stats_interval_before = sched.get_job(
        f"poll_stats_site_{site.id}",
    ).trigger.interval
    queries_interval_before = sched.get_job(
        f"poll_queries_site_{site.id}",
    ).trigger.interval

    reschedule_all_queries_jobs(sched, new_interval_seconds=30)

    stats_interval_after = sched.get_job(
        f"poll_stats_site_{site.id}",
    ).trigger.interval
    queries_interval_after = sched.get_job(
        f"poll_queries_site_{site.id}",
    ).trigger.interval

    assert stats_interval_after == stats_interval_before  # untouched
    assert queries_interval_after != queries_interval_before  # rescheduled


# ── prune_inactive_state — VIP path ──────────────────────────────────────────


async def test_prune_drops_vip_state_for_demoted_sites(instance, db_session):
    """When a site no longer has any vip-flagged instances, the VIP
    state dicts (which are keyed by site_id) shed those entries."""
    from app.services import collector
    from app.services.collector import prune_inactive_state

    # Pre-seed VIP state for a site with no VIP instances.
    site_id = instance.site_id
    collector._vip_active_node[site_id] = uuid.uuid4()
    collector._vip_group_stall_alerted[site_id] = False
    collector._site_poll_seq[site_id] = 5

    await prune_inactive_state()

    # No instance has vip_role set, so the site is no longer "VIP-tracked"
    # and the dicts must drop their entries for that site.
    assert site_id not in collector._vip_active_node
    assert site_id not in collector._vip_group_stall_alerted
    assert site_id not in collector._site_poll_seq
