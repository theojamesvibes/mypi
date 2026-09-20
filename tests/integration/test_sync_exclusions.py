"""Integration tests for keep-local config exclusions during a sync.

A teleporter import with config=True replaces a replica's whole
`pihole.toml`, so sync_service brackets the import: snapshot the pinned
keys off the replica, import, PATCH them back, then read back to prove
they stuck. These tests drive that bracket over a mocked Pi-hole HTTP
surface, including the paths where it goes wrong — the point of the
feature is that a replica's local DNS is never silently lost.
"""
from __future__ import annotations

import contextlib
import io
import zipfile

import httpx
import pytest

MASTER_URL = "http://master.test"
REPLICA_URL = "http://replica.test"

# The replica's own values, and the master's — deliberately different so a
# test can tell which survived.
REPLICA_CONFIG = {
    "dns": {
        "hosts": ["10.0.0.5 nas.lan"],
        "cnameRecords": ["www.lan,nas.lan"],
        "upstreams": ["192.168.1.1"],
    },
    "dhcp": {"active": True, "start": "10.0.0.100"},
}
MASTER_CONFIG = {
    "dns": {
        "hosts": ["10.9.9.9 master-only.lan"],
        "cnameRecords": [],
        "upstreams": ["9.9.9.9"],
    },
    "dhcp": {"active": False, "start": ""},
}


def _valid_teleporter_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pihole.toml", b"x" * 2048)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _reset_state():
    from app.services import sync_service
    sync_service._state_by_site.clear()
    sync_service._lock_by_site.clear()
    sync_service._schedule_by_site.clear()
    sync_service._last_blocklist_by_site.clear()
    yield


@pytest.fixture(autouse=True)
def _no_settle_delay(monkeypatch):
    """The 5 s FTL-restart settle is real-world necessary and pure dead
    time in a test."""
    from app.services import sync_service
    monkeypatch.setattr(sync_service, "_CONFIG_RESTORE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(sync_service, "_CONFIG_RESTORE_RETRY_DELAYS", (0, 0, 0))
    yield


@pytest.fixture(autouse=True)
def _stub_pushover(monkeypatch):
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
async def site(db_session):
    from app.models.site import Site
    s = Site(name="Test", slug="test", is_active=True, is_main=True)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def cluster(db_session, site):
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


def _mock_master(respx_mock):
    respx_mock.post(f"{MASTER_URL}/api/auth").respond(
        200, json={"session": {"sid": "master-sid"}}
    )
    respx_mock.post(f"{MASTER_URL}/api/action/gravity").respond(200)
    respx_mock.get(f"{MASTER_URL}/api/teleporter").respond(
        200, content=_valid_teleporter_zip()
    )


def _mock_replica_auth(respx_mock):
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )
    respx_mock.post(f"{REPLICA_URL}/api/teleporter").respond(200)


class _FakeReplicaConfig:
    """Stands in for a replica's pihole.toml across a whole sync.

    GET returns the current tree; the teleporter import stamps the
    master's values over it (as FTL does); PATCH merges a partial back.
    """

    def __init__(self):
        import copy
        self.tree = copy.deepcopy(REPLICA_CONFIG)
        self.patches: list[dict] = []

    def import_master(self, request=None):
        """What FTL does on a config import: the master's pihole.toml wins."""
        import copy
        self.tree = copy.deepcopy(MASTER_CONFIG)
        return httpx.Response(200)

    def get(self, request):
        import copy
        return httpx.Response(200, json={"config": copy.deepcopy(self.tree)})

    def patch(self, request):
        import json
        partial = json.loads(request.content)["config"]
        self.patches.append(partial)
        self._merge(self.tree, partial)
        return httpx.Response(200, json={"config": self.tree})

    @staticmethod
    def _merge(dst, src):
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                _FakeReplicaConfig._merge(dst[key], value)
            else:
                dst[key] = value


@pytest.fixture
def replica_config(respx_mock):
    """Wire a stateful replica config onto the respx mock."""
    state = _FakeReplicaConfig()
    respx_mock.get(f"{REPLICA_URL}/api/config").mock(side_effect=state.get)
    respx_mock.patch(f"{REPLICA_URL}/api/config").mock(side_effect=state.patch)
    respx_mock.post(f"{REPLICA_URL}/api/teleporter").mock(side_effect=state.import_master)
    return state


# ── The feature working ──────────────────────────────────────────────────────


async def test_pinned_keys_survive_the_import(cluster, respx_mock, replica_config):
    """The whole point: the replica keeps its own local DNS records and
    upstreams while everything else takes the master's copy."""
    from app.services.sync_service import run_sync

    master, _ = cluster
    _mock_master(respx_mock)
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )

    state = await run_sync(
        site_id=master.site_id,
        import_gravity=False,
        config_exclusions=["dns.hosts", "dns.upstreams"],
    )

    assert state.status == "success"
    assert state.results[0].status == "success"
    assert state.results[0].preserved_keys == ["dns.hosts", "dns.upstreams"]

    # The replica's own values are back...
    assert replica_config.tree["dns"]["hosts"] == REPLICA_CONFIG["dns"]["hosts"]
    assert replica_config.tree["dns"]["upstreams"] == REPLICA_CONFIG["dns"]["upstreams"]
    # ...and everything unpinned still took the master's copy.
    assert replica_config.tree["dns"]["cnameRecords"] == MASTER_CONFIG["dns"]["cnameRecords"]
    assert replica_config.tree["dhcp"] == MASTER_CONFIG["dhcp"]

    # One PATCH, carrying both keys in a single merged payload.
    assert replica_config.patches == [
        {"dns": {"hosts": REPLICA_CONFIG["dns"]["hosts"],
                 "upstreams": REPLICA_CONFIG["dns"]["upstreams"]}}
    ]


async def test_whole_section_can_be_pinned(cluster, respx_mock, replica_config):
    """"dhcp" pins every key under it — a replica's DHCP range is the
    classic thing that must never come from the master."""
    from app.services.sync_service import run_sync

    master, _ = cluster
    _mock_master(respx_mock)
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )

    state = await run_sync(
        site_id=master.site_id, import_gravity=False, config_exclusions=["dhcp"],
    )

    assert state.status == "success"
    assert replica_config.tree["dhcp"] == REPLICA_CONFIG["dhcp"]
    assert replica_config.tree["dns"]["hosts"] == MASTER_CONFIG["dns"]["hosts"]


async def test_exclusions_are_ignored_when_config_is_not_synced(cluster, respx_mock):
    """Nothing overwrites pihole.toml with config=False, so there is
    nothing to snapshot or restore — and no /api/config calls to make.
    The config routes are deliberately left unmocked: touching them would
    surface as an unmocked-request failure."""
    from app.services.sync_service import run_sync

    master, _ = cluster
    _mock_master(respx_mock)
    _mock_replica_auth(respx_mock)

    state = await run_sync(
        site_id=master.site_id,
        import_config=False,
        import_gravity=False,
        config_exclusions=["dns.hosts"],
    )

    assert state.status == "success"
    assert state.results[0].preserved_keys == []
    assert not [c for c in respx_mock.calls if c.request.url.path == "/api/config"]


async def test_unknown_key_is_skipped_without_failing_the_sync(
    cluster, respx_mock, replica_config,
):
    from app.services.sync_service import run_sync

    master, _ = cluster
    _mock_master(respx_mock)
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )

    state = await run_sync(
        site_id=master.site_id,
        import_gravity=False,
        config_exclusions=["dns.hosts", "dns.thisKeyDoesNotExist"],
    )

    assert state.status == "success"
    assert state.results[0].preserved_keys == ["dns.hosts"]
    assert replica_config.tree["dns"]["hosts"] == REPLICA_CONFIG["dns"]["hosts"]


async def test_saved_site_exclusions_apply_when_the_caller_omits_them(
    cluster, respx_mock, replica_config,
):
    """A client that predates this feature (or the iOS app) sends no
    exclusion list — the site's saved keys must still be honoured, or a
    scheduled sync would quietly undo what the user pinned in the UI."""
    from app.services import sync_service

    master, _ = cluster
    _mock_master(respx_mock)
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )

    sync_service._get_schedule_config(master.site_id)["config_exclusions"] = ["dns.hosts"]

    # No config_exclusions argument at all — the saved list must be used.
    state = await sync_service.run_sync(site_id=master.site_id, import_gravity=False)

    assert state.results[0].preserved_keys == ["dns.hosts"]
    assert replica_config.tree["dns"]["hosts"] == REPLICA_CONFIG["dns"]["hosts"]


# ── The feature failing safely ───────────────────────────────────────────────


async def test_unreadable_replica_config_aborts_that_replicas_import(
    cluster, respx_mock,
):
    """If we can't snapshot the pinned keys we must not import: the import
    is the point of no return, and the keys would be gone for good."""
    from app.services.sync_service import run_sync

    master, _ = cluster
    _mock_master(respx_mock)
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )
    respx_mock.get(f"{REPLICA_URL}/api/config").respond(500)
    # No teleporter route for the replica: the import must never be reached.

    state = await run_sync(
        site_id=master.site_id, import_gravity=False, config_exclusions=["dns.hosts"],
    )

    assert state.status == "error"
    assert state.results[0].status == "error"
    assert "dns.hosts" in state.results[0].error
    replica_imports = [
        c for c in respx_mock.calls
        if c.request.url.path == "/api/teleporter" and c.request.method == "POST"
    ]
    assert not replica_imports, "import must not run when the snapshot failed"


async def test_restore_that_does_not_stick_is_reported_as_an_error(
    cluster, respx_mock,
):
    """A 200 from PATCH is not proof FTL kept the value. If the read-back
    shows the master's value still in place, the replica is an error —
    reporting success on a replica whose local DNS was just overwritten is
    the one outcome worse than a failed sync."""
    from app.services.sync_service import run_sync

    master, _ = cluster
    _mock_master(respx_mock)
    _mock_replica_auth(respx_mock)

    responses = [
        # Snapshot: the replica's own records.
        httpx.Response(200, json={"config": REPLICA_CONFIG}),
        # Read-back after the restore: still the master's. FTL ignored us.
        httpx.Response(200, json={"config": MASTER_CONFIG}),
    ]
    respx_mock.get(f"{REPLICA_URL}/api/config").mock(
        side_effect=lambda request: responses.pop(0)
    )
    respx_mock.patch(f"{REPLICA_URL}/api/config").respond(200, json={"config": {}})

    state = await run_sync(
        site_id=master.site_id, import_gravity=False, config_exclusions=["dns.hosts"],
    )

    assert state.status == "error"
    assert "did not stick" in state.results[0].error


async def test_rejected_batch_patch_falls_back_to_one_key_at_a_time(
    cluster, respx_mock,
):
    """One read-only or FTLCONF_-forced key must not cost the user the
    other keys they pinned."""
    from app.services.sync_service import run_sync

    master, _ = cluster
    _mock_master(respx_mock)
    _mock_replica_auth(respx_mock)
    respx_mock.get(f"{REPLICA_URL}/api/config").respond(
        200, json={"config": REPLICA_CONFIG}
    )

    seen: list[dict] = []

    def _patch(request):
        import json
        partial = json.loads(request.content)["config"]
        seen.append(partial)
        # Reject the batch (both keys), accept either key on its own.
        if len(partial.get("dns", {})) > 1:
            return httpx.Response(400, json={"error": "read-only item"})
        return httpx.Response(200, json={"config": {}})

    respx_mock.patch(f"{REPLICA_URL}/api/config").mock(side_effect=_patch)

    state = await run_sync(
        site_id=master.site_id,
        import_gravity=False,
        config_exclusions=["dns.hosts", "dns.upstreams"],
    )

    # Batch attempt, then one PATCH per key.
    assert len(seen) == 3
    assert seen[1] == {"dns": {"hosts": REPLICA_CONFIG["dns"]["hosts"]}}
    assert seen[2] == {"dns": {"upstreams": REPLICA_CONFIG["dns"]["upstreams"]}}
    assert state.status == "success"


# ── FTL still restarting when the restore fires ──────────────────────────────


async def test_restore_waits_out_an_ftl_restart(cluster, respx_mock, replica_config):
    """Seen live: the PATCH fired 5 s after the import, FTL was still
    restarting, the connection was refused, and the replica lost its local
    DNS. A refused connection must be retried, not treated as final."""
    from app.services import sync_service

    master, replica = cluster
    _mock_master(respx_mock)
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )

    attempts = 0

    def _patch(request):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise httpx.ConnectError("All connection attempts failed")
        return replica_config.patch(request)

    respx_mock.patch(f"{REPLICA_URL}/api/config").mock(side_effect=_patch)

    state = await sync_service.run_sync(
        site_id=master.site_id, import_gravity=False, config_exclusions=["dns.hosts"],
    )

    assert attempts == 3
    assert state.status == "success"
    assert replica_config.tree["dns"]["hosts"] == REPLICA_CONFIG["dns"]["hosts"]
    # Verified, so nothing is left for a later sync to finish.
    assert await sync_service._load_pending_restore(replica) is None


async def test_next_sync_finishes_a_restore_that_never_connected(
    cluster, respx_mock, replica_config,
):
    """If the replica stays down for the whole retry window its keys are
    overwritten — but the snapshot was persisted before the import, so the
    next sync must restore the replica's own values rather than snapshot
    the master's copy and "preserve" that for ever after."""
    from app.services import sync_service

    master, replica = cluster
    _mock_master(respx_mock)
    respx_mock.get(f"{MASTER_URL}/api/config").respond(200, json={"config": MASTER_CONFIG})
    respx_mock.post(f"{REPLICA_URL}/api/auth").respond(
        200, json={"session": {"sid": "replica-sid"}}
    )

    ftl_down = True

    def _patch(request):
        if ftl_down:
            raise httpx.ConnectError("All connection attempts failed")
        return replica_config.patch(request)

    respx_mock.patch(f"{REPLICA_URL}/api/config").mock(side_effect=_patch)

    first = await sync_service.run_sync(
        site_id=master.site_id, import_gravity=False, config_exclusions=["dns.hosts"],
    )
    assert first.status == "error"
    assert "next sync" in first.results[0].error
    assert replica_config.tree["dns"]["hosts"] == MASTER_CONFIG["dns"]["hosts"]
    pending = await sync_service._load_pending_restore(replica)
    assert pending["payload"] == {"dns": {"hosts": REPLICA_CONFIG["dns"]["hosts"]}}

    ftl_down = False
    second = await sync_service.run_sync(
        site_id=master.site_id, import_gravity=False, config_exclusions=["dns.hosts"],
    )

    assert second.status == "success"
    assert replica_config.tree["dns"]["hosts"] == REPLICA_CONFIG["dns"]["hosts"]
    assert await sync_service._load_pending_restore(replica) is None


def test_pending_restore_does_not_override_a_hand_edit():
    """A saved snapshot only wins where the replica is still serving the
    master's value. If the live value is neither the master's nor the saved
    one, someone fixed or changed it by hand since — leave it alone."""
    from app.services.sync_service import recover_pending_restore

    pending = {
        "payload": {"dns": {"hosts": ["10.0.0.5 nas.lan"], "upstreams": ["127.0.0.1#5335"]}},
        "paths": ["dns.hosts", "dns.upstreams"],
    }
    live = {"dns": {"hosts": ["10.0.0.6 edited.lan"], "upstreams": ["127.0.0.1#5353"]}}
    master_cfg = {"dns": {"hosts": ["10.9.9.9 master-only.lan"], "upstreams": ["127.0.0.1#5353"]}}

    payload, found = recover_pending_restore(
        live, ["dns.hosts", "dns.upstreams"], pending, master_cfg,
        ["dns.hosts", "dns.upstreams"], "replica",
    )

    assert payload["dns"]["hosts"] == ["10.0.0.6 edited.lan"]      # hand edit kept
    assert payload["dns"]["upstreams"] == ["127.0.0.1#5335"]       # clobbered → recovered
    assert found == ["dns.hosts", "dns.upstreams"]


def test_pending_restore_ignores_keys_no_longer_pinned():
    from app.services.sync_service import recover_pending_restore

    pending = {"payload": {"dns": {"upstreams": ["127.0.0.1#5335"]}}, "paths": ["dns.upstreams"]}

    payload, found = recover_pending_restore({}, [], pending, None, ["dns.hosts"], "replica")

    assert payload == {}
    assert found == []
