"""Integration tests for sync_service.run_sync orchestration.

run_sync is the multi-step ritual that has bitten MyPi in production
several times — gravity on master → teleporter export → broadcast to
replicas → gravity on replicas. Mocks the Pi-hole HTTP surface with
respx; the master + replicas live in a real testcontainer DB.
"""
from __future__ import annotations

import contextlib
import io
import zipfile

import pytest


def _valid_teleporter_zip() -> bytes:
    """Construct a >1KB valid ZIP archive that the validator accepts.
    Padding is needed because _validate_teleporter_zip refuses
    sub-1KB responses (truncation guard)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pihole.toml", b"x" * 2048)
    return buf.getvalue()


# ── Reset collector + sync state between tests ───────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state():
    from app.services import collector, sync_service
    for d in (
        collector._consec_failures, collector._cooldown_until,
        collector._last_failure_at,
    ):
        d.clear()
    sync_service._state_by_site.clear()
    sync_service._lock_by_site.clear()
    sync_service._last_blocklist_by_site.clear()
    yield


# ── Mute Pushover ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_pushover(monkeypatch):
    """run_sync calls pushover_service.notify_sync_failure on errors —
    the Pushover client would try to make outbound HTTP calls. Stub
    every notify_* function to a no-op so tests don't need a Pushover
    mock alongside the Pi-hole respx mocks."""
    from app.services import pushover

    async def _noop(*args, **kwargs):
        return None

    for name in (
        "notify_sync_failure", "notify_instance_offline",
        "notify_instance_back_online", "notify_instance_stalled",
        "notify_instance_recovered_from_stall",
        "notify_vip_transfer", "notify_vip_group_stalled",
        "notify_vip_group_recovered",
    ):
        monkeypatch.setattr(pushover, name, _noop)
    yield


# ── Site / master / replica setup ────────────────────────────────────────────


MASTER_URL = "http://master.test"
REPLICA_URL = "http://replica.test"
REPLICA2_URL = "http://replica2.test"


@pytest.fixture
async def site(db_session):
    from app.models.site import Site
    s = Site(name="Test", slug="test", is_active=True, is_main=True)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def cluster(db_session, site):
    """One master + one replica with respx-friendly URLs."""
    from cryptography.fernet import Fernet

    import app.models.pihole as pihole_models
    from app.config import settings
    from app.models.pihole import PiholeInstance

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    master = PiholeInstance(
        site_id=site.id, name="master", url=MASTER_URL,
        api_password="pw", is_master=True, is_active=True,
    )
    replica = PiholeInstance(
        site_id=site.id, name="replica", url=REPLICA_URL,
        api_password="pw", is_master=False, is_active=True,
    )
    db_session.add_all([master, replica])
    await db_session.commit()
    await db_session.refresh(master)
    await db_session.refresh(replica)
    return master, replica


@pytest.fixture
async def cluster_two_replicas(db_session, site):
    """One master + two replicas — for partial-failure scenarios where
    one replica must succeed while the other errors."""
    from cryptography.fernet import Fernet

    import app.models.pihole as pihole_models
    from app.config import settings
    from app.models.pihole import PiholeInstance

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    master = PiholeInstance(
        site_id=site.id, name="master", url=MASTER_URL,
        api_password="pw", is_master=True, is_active=True,
    )
    replica = PiholeInstance(
        site_id=site.id, name="replica", url=REPLICA_URL,
        api_password="pw", is_master=False, is_active=True,
    )
    replica2 = PiholeInstance(
        site_id=site.id, name="replica2", url=REPLICA2_URL,
        api_password="pw", is_master=False, is_active=True,
    )
    db_session.add_all([master, replica, replica2])
    await db_session.commit()
    await db_session.refresh(master)
    await db_session.refresh(replica)
    await db_session.refresh(replica2)
    return master, replica, replica2


@pytest.fixture(autouse=True)
async def _clear_client_registry():
    """run_sync uses get_client → reuses a persistent PiholeClient
    across tests. Wipe it so respx routes set up by the next test
    aren't shadowed by the old client's open httpx instance."""
    from app.services import client_manager

    client_manager._clients.clear()
    client_manager._last_persisted_sid.clear()
    yield
    for key in list(client_manager._clients):
        with contextlib.suppress(Exception):
            await client_manager._clients[key].close()
    client_manager._clients.clear()
    client_manager._last_persisted_sid.clear()


# ── Happy path ───────────────────────────────────────────────────────────────


async def test_run_sync_happy_path(cluster, respx_mock):
    """Master → 1 replica, every Pi-hole call succeeds. Final state
    is `success` and both rows show success."""
    from app.services.sync_service import run_sync

    master, replica = cluster

    # Master endpoints.
    respx_mock.post(f"{MASTER_URL}/api/auth").respond(
        200, json={"session": {"sid": "master-sid"}}
    )
    respx_mock.post(f"{MASTER_URL}/api/action/gravity").respond(200)
    respx_mock.get(f"{MASTER_URL}/api/teleporter").respond(
        200, content=_valid_teleporter_zip()
    )

    # Replica endpoints.
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )
    respx_mock.post(f"{REPLICA_URL}/api/teleporter").respond(200)
    respx_mock.post(f"{REPLICA_URL}/api/action/gravity").respond(200)

    state = await run_sync(site_id=master.site_id)

    assert state.status == "success"
    assert state.master == "master"
    assert len(state.results) == 1
    assert state.results[0].name == "replica"
    assert state.results[0].status == "success"


async def test_run_sync_records_replica_failure(cluster, respx_mock):
    """Replica's teleporter import fails on both attempts (transient
    + retry). Overall sync state is `error`, the replica's row is
    flagged with the error string. Master is still reported success."""
    from app.services.sync_service import run_sync

    master, replica = cluster

    respx_mock.post(f"{MASTER_URL}/api/auth").respond(
        200, json={"session": {"sid": "master-sid"}}
    )
    respx_mock.post(f"{MASTER_URL}/api/action/gravity").respond(200)
    respx_mock.get(f"{MASTER_URL}/api/teleporter").respond(
        200, content=_valid_teleporter_zip()
    )

    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )
    # Both attempts fail with non-transient 500 — no retry path.
    respx_mock.post(f"{REPLICA_URL}/api/teleporter").respond(500)

    state = await run_sync(site_id=master.site_id)

    assert state.status == "error"
    assert len(state.results) == 1
    assert state.results[0].status == "error"
    assert state.results[0].error  # non-empty


async def test_run_sync_partial_failure_one_replica_errors(
    cluster_two_replicas, respx_mock, monkeypatch,
):
    """Master exports fine; replica succeeds, replica2's teleporter
    import raises ConnectError on both the first attempt and the
    transient-retry. Contract (per run_sync): overall status is
    `error` whenever *any* per-replica result is an error, but the
    top-level `state.error` stays None — the failure detail lives in
    the per-instance results. The good replica must still be synced
    and reported `success`, completed_at must be set, and pushover's
    notify_sync_failure must fire with the failed replica in the body
    (the results-derived branch, since state.error is None)."""
    import asyncio

    import httpx

    from app.services import pushover
    from app.services.sync_service import run_sync

    master, replica, replica2 = cluster_two_replicas

    notified: list[tuple[str, dict]] = []

    async def _record_failure(body, **kwargs):
        notified.append((body, kwargs))

    # Override the autouse no-op stub with a recorder for this test.
    monkeypatch.setattr(pushover, "notify_sync_failure", _record_failure)

    # Master endpoints.
    respx_mock.post(f"{MASTER_URL}/api/auth").respond(
        200, json={"session": {"sid": "master-sid"}}
    )
    respx_mock.post(f"{MASTER_URL}/api/action/gravity").respond(200)
    respx_mock.get(f"{MASTER_URL}/api/teleporter").respond(
        200, content=_valid_teleporter_zip()
    )

    # Healthy replica.
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )
    good_teleporter = respx_mock.post(f"{REPLICA_URL}/api/teleporter").respond(200)

    # Broken replica: ConnectError is in _TRANSIENT_SYNC_ERRORS, so
    # run_sync evicts the client and retries once — both attempts fail.
    respx_mock.post(f"{REPLICA2_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica2-sid"}}
    )
    respx_mock.post(f"{REPLICA2_URL}/api/teleporter").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    # import_gravity=False skips the 5s pre-gravity sleep per replica —
    # the partial-failure contract under test is identical either way.
    state = await run_sync(site_id=master.site_id, import_gravity=False)

    assert state.status == "error"
    assert state.error is None  # detail is per-instance, not top-level
    assert state.master == "master"
    assert state.completed_at is not None

    by_name = {r.name: r for r in state.results}
    assert set(by_name) == {"replica", "replica2"}
    assert by_name["replica"].status == "success"
    assert by_name["replica"].error is None
    assert by_name["replica2"].status == "error"
    assert by_name["replica2"].error and "connection refused" in by_name["replica2"].error
    # The good replica's import actually happened — partial failure
    # must not abort the broadcast to healthy replicas.
    assert good_teleporter.called

    # notify_sync_failure is fired via _spawn — yield to the loop.
    await asyncio.sleep(0.05)
    assert len(notified) == 1
    body, kwargs = notified[0]
    assert "replica2" in body and "connection refused" in body
    assert kwargs.get("site_id") == master.site_id


async def test_run_sync_aborts_when_master_export_fails(cluster, respx_mock):
    """Master's teleporter endpoint is broken — sync_service retries
    once, then surfaces a `RuntimeError` and parks the state at
    `error`. No replica write may be attempted: a failed export must
    never fan anything out."""
    from app.services.sync_service import run_sync

    master, _ = cluster

    respx_mock.post(f"{MASTER_URL}/api/auth").respond(
        200, json={"session": {"sid": "master-sid"}}
    )
    respx_mock.post(f"{MASTER_URL}/api/action/gravity").respond(200)
    # Both attempts return 500 — no usable export.
    respx_mock.get(f"{MASTER_URL}/api/teleporter").respond(500)

    # Register replica routes so any (incorrect) replica write would be
    # captured rather than erroring as an unmocked host.
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )
    replica_teleporter = respx_mock.post(f"{REPLICA_URL}/api/teleporter").respond(200)

    state = await run_sync(site_id=master.site_id)
    assert state.status == "error"
    assert state.error is not None and "teleporter" in state.error.lower()
    assert state.completed_at is not None
    # The export never succeeded, so nothing was pushed to replicas.
    assert state.results == []
    assert not replica_teleporter.called


# ── notify_blocklist_count → auto-sync ───────────────────────────────────────


async def test_notify_blocklist_count_initial_call_is_noop(cluster, respx_mock):
    """First observation establishes the watermark — even with auto-
    sync enabled, no sync runs because there's nothing to compare."""
    from app.services import sync_service

    master, _ = cluster
    sync_service._schedule_by_site[str(master.site_id)] = {
        "interval_minutes": 0, "auto_gravity": True,
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    }

    triggered = []

    async def _record(**kwargs):
        triggered.append(kwargs)

    # Replace run_sync so we just record the trigger instead of doing
    # the full orchestration here.
    import app.services.sync_service as ss
    original = ss.run_sync
    ss.run_sync = _record  # type: ignore[assignment]
    try:
        await ss.notify_blocklist_count(master.site_id, count=1000)
    finally:
        ss.run_sync = original

    assert triggered == []
    assert sync_service._last_blocklist_by_site[str(master.site_id)] == 1000


async def test_notify_blocklist_count_change_triggers_auto_sync(cluster):
    """Second observation with a different count fires run_sync via
    _spawn (when auto_gravity is enabled)."""
    from app.services import sync_service

    master, _ = cluster
    sync_service._schedule_by_site[str(master.site_id)] = {
        "interval_minutes": 0, "auto_gravity": True,
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    }

    triggered = []

    async def _record(**kwargs):
        triggered.append(kwargs)

    import app.services.sync_service as ss
    original = ss.run_sync
    ss.run_sync = _record  # type: ignore[assignment]
    try:
        # First call seeds the watermark.
        await ss.notify_blocklist_count(master.site_id, count=1000)
        assert triggered == []
        # Second call with a different count fires auto-sync.
        await ss.notify_blocklist_count(master.site_id, count=1100)
        # _spawn schedules a task; let the loop run it.
        import asyncio
        await asyncio.sleep(0.05)
    finally:
        ss.run_sync = original

    assert len(triggered) == 1
    assert triggered[0]["site_id"] == master.site_id


async def test_notify_blocklist_count_no_auto_sync_when_disabled(cluster):
    """With auto_gravity=False, even a count change must not trigger
    a sync — the blocklist watermark is still updated for next time."""
    from app.services import sync_service

    master, _ = cluster
    sync_service._schedule_by_site[str(master.site_id)] = {
        "interval_minutes": 0, "auto_gravity": False,
        "import_config": True, "import_gravity": True,
        "import_dhcp_leases": False, "run_gravity": True,
    }

    triggered = []

    async def _record(**kwargs):
        triggered.append(kwargs)

    import app.services.sync_service as ss
    original = ss.run_sync
    ss.run_sync = _record  # type: ignore[assignment]
    try:
        await ss.notify_blocklist_count(master.site_id, count=1000)
        await ss.notify_blocklist_count(master.site_id, count=1100)
    finally:
        ss.run_sync = original

    assert triggered == []
