"""Tests for the per-instance + per-site state machines that drive
collector.py.

These functions are technically not pure (they read/write module-level
dicts) but they don't touch the DB or the network, so they go in the
fast service-layer suite. The autouse `_reset_collector_state` fixture
in conftest.py clears every dict between tests.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import collector
from app.models.pihole import StatsSnapshot


# ── Helpers ──────────────────────────────────────────────────────────────────


class _StubInstance:
    """Stand-in for app.models.pihole.PiholeInstance — only the attrs
    that the state-machine helpers read."""

    def __init__(
        self,
        instance_id: uuid.UUID | None = None,
        name: str = "p1",
        vip_role: str | None = None,
        site_id: uuid.UUID | None = None,
    ):
        self.id = instance_id or uuid.uuid4()
        self.name = name
        self.vip_role = vip_role
        self.site_id = site_id or uuid.uuid4()


def _snap(count: int = 0, status: str = "online") -> StatsSnapshot:
    return StatsSnapshot(
        instance_id=uuid.uuid4(),
        collected_at=datetime.now(timezone.utc),
        status=status,
        dns_queries_today=count,
    )


# ── Circuit breaker ──────────────────────────────────────────────────────────


def test_breaker_allows_when_no_cooldown():
    assert collector._breaker_allows("k", "name") is True


def test_breaker_blocks_during_active_cooldown():
    collector._cooldown_until["k"] = datetime.now(timezone.utc) + timedelta(seconds=300)
    assert collector._breaker_allows("k", "name") is False


def test_breaker_allows_after_cooldown_expires():
    collector._cooldown_until["k"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert collector._breaker_allows("k", "name") is True


def test_breaker_failure_increments_then_trips_at_threshold():
    """N consecutive failures past the threshold open the breaker."""
    threshold = collector._CIRCUIT_FAIL_THRESHOLD
    # Push _last_failure_at backwards each iteration so the dedup window
    # doesn't swallow the increments.
    for _ in range(threshold):
        # Force the dedup check to "old enough" before each failure call.
        collector._last_failure_at["k"] = time.monotonic() - 10
        collector._breaker_failure("k", "name")
    # After threshold consecutive failures, cooldown is armed.
    assert "k" in collector._cooldown_until


def test_breaker_failure_dedups_within_window():
    """Stats + queries polls share the same TCP connection, so a single
    network blip surfaces as two simultaneous failures. The dedup
    window collapses them so the breaker doesn't trip on the first
    real hiccup."""
    collector._breaker_failure("k", "name")
    first_count = collector._consec_failures["k"]
    # Immediately call again — within dedup window, must NOT increment.
    collector._breaker_failure("k", "name")
    assert collector._consec_failures["k"] == first_count


def test_breaker_success_clears_all_state():
    collector._consec_failures["k"] = 2
    collector._cooldown_until["k"] = datetime.now(timezone.utc) + timedelta(seconds=300)
    collector._last_failure_at["k"] = time.monotonic()

    collector._breaker_success("k", "name")

    assert "k" not in collector._consec_failures
    assert "k" not in collector._cooldown_until
    assert "k" not in collector._last_failure_at


# ── _instance_advanced ───────────────────────────────────────────────────────


def test_instance_advanced_returns_none_on_first_poll():
    """No prior baseline means we can't tell — return None so the
    stall detector treats it as a bootstrap poll."""
    inst = _StubInstance()
    snap = _snap(count=100)
    result = collector._instance_advanced(inst, snap)
    assert result is None


def test_instance_advanced_returns_true_when_counter_grew():
    inst = _StubInstance()
    collector._instance_advanced(inst, _snap(count=100))  # establish baseline
    result = collector._instance_advanced(inst, _snap(count=110))
    assert result is True


def test_instance_advanced_returns_false_when_flat():
    inst = _StubInstance()
    collector._instance_advanced(inst, _snap(count=100))  # baseline
    result = collector._instance_advanced(inst, _snap(count=100))  # no change
    assert result is False


def test_instance_advanced_treats_counter_rollover_as_reset():
    """At midnight, dns_queries_today rolls back to 0. That's not a
    stall — clear pending stall state and return False."""
    inst = _StubInstance()
    collector._instance_advanced(inst, _snap(count=100_000))  # busy day
    collector._stall_count[str(inst.id)] = 3  # in-flight stall counter
    collector._stall_alerted[str(inst.id)] = True

    result = collector._instance_advanced(inst, _snap(count=50))  # rollover
    assert result is False
    assert str(inst.id) not in collector._stall_count
    assert str(inst.id) not in collector._stall_alerted


def test_instance_advanced_uses_watermark_when_counter_flat():
    """A flat dns_queries_today + a moved query watermark still counts
    as advancement — the instance is processing traffic."""
    inst = _StubInstance()
    collector._last_seen_ts[str(inst.id)] = 1000.0
    collector._instance_advanced(inst, _snap(count=100))  # baseline

    # Counter unchanged but watermark moved forward.
    collector._last_seen_ts[str(inst.id)] = 2000.0
    result = collector._instance_advanced(inst, _snap(count=100))
    assert result is True


# ── _check_stalled ───────────────────────────────────────────────────────────


def test_check_stalled_skips_vip_instances():
    """Stall detection for VIP-grouped instances is handled by
    _check_vip_state instead — per-instance check would false-fire on
    legitimately-idle standbys."""
    inst = _StubInstance(vip_role="replica")
    collector._check_stalled(inst, advanced=False, site_name="test")
    assert str(inst.id) not in collector._stall_count


def test_check_stalled_skips_bootstrap_poll():
    inst = _StubInstance()
    collector._check_stalled(inst, advanced=None, site_name="test")
    assert str(inst.id) not in collector._stall_count


def test_check_stalled_increments_then_alerts(monkeypatch):
    """After _STALL_THRESHOLD_POLLS consecutive flat polls, the alert
    fires exactly once (via _spawn → pushover)."""
    spawned = []
    monkeypatch.setattr(collector, "_spawn", lambda coro: spawned.append(coro))

    inst = _StubInstance()
    threshold = collector._STALL_THRESHOLD_POLLS

    for i in range(threshold - 1):
        collector._check_stalled(inst, advanced=False, site_name="test")
        assert str(inst.id) not in collector._stall_alerted, (
            f"alerted too early at poll {i + 1}/{threshold}"
        )

    # Threshold-th flat poll triggers the alert.
    collector._check_stalled(inst, advanced=False, site_name="test")
    assert collector._stall_alerted[str(inst.id)] is True
    assert len(spawned) == 1

    # Close the unawaited coroutines so pytest doesn't warn.
    for c in spawned:
        c.close()


# ── _check_vip_state ─────────────────────────────────────────────────────────


async def test_vip_transfer_requires_5_confirming_polls(monkeypatch):
    """A standby that briefly serves a few queries must NOT trigger a
    transfer alert — only sustained advancement past the confirm
    threshold counts. Replaces the dev.20-era 2-poll false-positive
    bug."""
    spawned = []
    monkeypatch.setattr(collector, "_spawn", lambda coro: spawned.append(coro))

    site_id = uuid.uuid4()
    master = _StubInstance(name="m", vip_role="master", site_id=site_id)
    replica = _StubInstance(name="r", vip_role="replica", site_id=site_id)

    # Bootstrap: master is the active node.
    outcomes = [
        (master, _snap(), True),    # master advances → seeds active
        (replica, _snap(), False),
    ]
    await collector._check_vip_state(site_id, "site", outcomes, configured_vip_count=2)
    assert collector._vip_active_node[site_id] == master.id

    # Now master goes idle, replica advances — but only 4 polls in a
    # row, one short of the confirmation threshold.
    confirm = collector._VIP_TRANSFER_CONFIRM_POLLS
    for _ in range(confirm - 1):
        outcomes = [
            (master, _snap(), False),
            (replica, _snap(), True),
        ]
        await collector._check_vip_state(site_id, "site", outcomes, configured_vip_count=2)

    # Active node has not transferred yet.
    assert collector._vip_active_node[site_id] == master.id
    transfer_calls = [c for c in spawned]
    for c in transfer_calls:
        c.close()
    assert len(transfer_calls) == 0


async def test_vip_transfer_fires_after_confirm_threshold(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(collector, "_spawn", lambda coro: spawned.append(coro))

    site_id = uuid.uuid4()
    master = _StubInstance(name="m", vip_role="master", site_id=site_id)
    replica = _StubInstance(name="r", vip_role="replica", site_id=site_id)

    # Seed master as active.
    await collector._check_vip_state(
        site_id, "site",
        [(master, _snap(), True), (replica, _snap(), False)],
        configured_vip_count=2,
    )

    confirm = collector._VIP_TRANSFER_CONFIRM_POLLS
    for _ in range(confirm):
        outcomes = [
            (master, _snap(), False),
            (replica, _snap(), True),
        ]
        await collector._check_vip_state(site_id, "site", outcomes, configured_vip_count=2)

    assert collector._vip_active_node[site_id] == replica.id
    assert len(spawned) >= 1
    for c in spawned:
        c.close()


async def test_vip_group_stall_requires_every_node_observed_online(monkeypatch):
    """1.11.0-dev.20 fix: group-stall must NOT fire when one VIP node
    is offline (excluded from poll_outcomes). Treating a missing node
    as 'flat' produced false positives during transient master TLS
    blips. Per-instance offline alerts cover real outages."""
    spawned: list = []
    monkeypatch.setattr(collector, "_spawn", lambda coro: spawned.append(coro))

    site_id = uuid.uuid4()
    master = _StubInstance(name="m", vip_role="master", site_id=site_id)
    replica = _StubInstance(name="r", vip_role="replica", site_id=site_id)

    # Both nodes advance once so `ever_advanced` becomes True (group
    # stall can only fire on a cluster that has historical activity).
    await collector._check_vip_state(
        site_id, "site",
        [(master, _snap(), True), (replica, _snap(), True)],
        configured_vip_count=2,
    )

    # Now report only ONE node — simulating the master being offline
    # and dropped from poll_outcomes. configured_vip_count stays at 2.
    threshold = collector._STALL_THRESHOLD_POLLS
    for _ in range(threshold + 2):
        await collector._check_vip_state(
            site_id, "site",
            [(replica, _snap(), False)],  # only replica observed
            configured_vip_count=2,
        )

    # Group-stall must NOT have fired — too few nodes observed.
    assert collector._vip_group_stall_alerted.get(site_id, False) is False
    for c in spawned:
        c.close()


async def test_vip_group_stall_fires_when_all_nodes_flat(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(collector, "_spawn", lambda coro: spawned.append(coro))

    site_id = uuid.uuid4()
    master = _StubInstance(name="m", vip_role="master", site_id=site_id)
    replica = _StubInstance(name="r", vip_role="replica", site_id=site_id)

    # Establish baseline activity (so `ever_advanced` is True).
    await collector._check_vip_state(
        site_id, "site",
        [(master, _snap(), True), (replica, _snap(), True)],
        configured_vip_count=2,
    )

    # Now both nodes go flat for the threshold-many polls.
    threshold = collector._STALL_THRESHOLD_POLLS
    for _ in range(threshold + 1):
        await collector._check_vip_state(
            site_id, "site",
            [(master, _snap(), False), (replica, _snap(), False)],
            configured_vip_count=2,
        )

    assert collector._vip_group_stall_alerted[site_id] is True
    for c in spawned:
        c.close()
