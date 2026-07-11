"""Shared per-instance / per-site runtime state and the circuit breaker.

Every dict here is keyed by `str(instance.id)` unless noted. State is
process-local and intentionally bare: it resets on restart, and the
`prune_inactive_state` job (maintenance.py) drops entries for instances
that config_loader has deactivated.

Sibling modules must access anything tests monkeypatch (`_spawn`) via
`state._spawn(...)` attribute lookup, not a from-import, so patches take
effect. The dicts themselves are only ever mutated in place (never
rebound), so from-imports of the dict objects are safe.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from app.config import settings

logger = logging.getLogger(__name__)

# Most recent query timestamp seen per instance (Unix float).
# Used to fetch only queries newer than what we already have.
_last_seen_ts: dict[str, float] = {}

# Previous poll status — used to detect online→offline transitions for alerts.
# Pruned alongside `_last_seen_ts` by `prune_inactive_state`, so this doesn't
# accumulate entries for deactivated instances.
_prev_status: dict[str, str] = {}

# Consecutive offline polls already retried before the first alert fires.
# Resets on transition (online→offline) and on recovery.
_offline_retry_count: dict[str, int] = {}

# Number of offline alerts already sent per instance in the current outage period.
# Resets to 0 when the instance recovers.  Used to enforce offline_alert_max_count.
_offline_alert_count: dict[str, int] = {}

# Stalled-state tracking. Catches the "split-state" failure mode where a
# Pi-hole's admin/web API keeps responding (so the offline check passes) but
# the underlying FTL has stopped logging queries — DNS port 53 may be dead
# or the query-log subsystem may have wedged, depending on which subsystem
# of FTL got stuck. Symptom: dns_queries_today counter and the per-instance
# query watermark both stop advancing while stats polls keep succeeding.
# Seen in the wild after the v6.6.0 → 6.6.1 upgrade left pihole1 half-up
# (incident on 2026-04-24, see project_pihole_upgrade_split_state.md).
_prev_dns_queries_today: dict[str, int] = {}
_stall_count: dict[str, int] = {}
_stall_alerted: dict[str, bool] = {}

# Tracks the watermark seen at the moment of the last stats poll, so the
# stall check compares "watermark now" vs "watermark last poll" rather than
# "watermark now" vs "earliest watermark ever". A poll that actually advances
# the watermark therefore clears stall state on the very next poll, even if
# the instance was previously busy and `_last_seen_ts` was already populated.
_prev_watermark_for_stall: dict[str, float | None] = {}

# Number of consecutive online polls with neither dns_queries_today nor the
# query watermark advancing before we flag an instance as stalled. With the
# 60-second poll interval this is 5 minutes — long enough that low-traffic
# instances with brief idle gaps don't trip it, short enough that a real
# wedge gets caught well before a human notices.
_STALL_THRESHOLD_POLLS = 5

# VIP cluster state. When `vip_master` / `vip_replica` are set in YAML, the
# collector tracks which node in the cluster is currently serving traffic
# and emits a "VIP transfer" alert when the active node changes (from master
# to replica or back). Per-instance stall alerts are suppressed for any
# instance with a non-NULL vip_role — idle is normal on a standby — but a
# group-level stall fires if every node in the cluster is flat at once
# (the whole VIP is dead).
#
# `_vip_last_advance_seq[instance_key]` — site-poll sequence at which this
#   instance last advanced its counter or watermark. Used by the per-site
#   VIP check to identify the most-recently-active node.
# `_vip_active_node[site_id]` — which instance the cluster is currently
#   considered to be serving from. Initially seeded from the configured
#   vip_master on first observation.
# `_vip_advance_streak[instance_key]` — consecutive site-polls this VIP
#   instance has advanced. A transfer alert requires a streak of >=
#   `_VIP_TRANSFER_CONFIRM_POLLS` so a single hiccup doesn't bounce the
#   active node back and forth.
# `_vip_group_stall_alerted[site_id]` — set true after the cluster-stall
#   alert has fired; cleared when any node advances again.
# `_site_poll_seq[site_id]` — incrementing counter, one per completed
#   `poll_stats_for_site` run. Used as the time axis for VIP tracking so
#   the logic doesn't depend on wall clock.
_vip_last_advance_seq: dict[str, int] = {}
_vip_active_node: dict[uuid.UUID, uuid.UUID | None] = {}
_vip_advance_streak: dict[str, int] = {}
_vip_group_stall_alerted: dict[uuid.UUID, bool] = {}
_site_poll_seq: dict[uuid.UUID, int] = {}

# Polls of sustained advance required on a candidate before we declare the
# active node has shifted. With a 60s poll interval, 5 polls = ~5 min —
# loose enough that a longer-lived blip on the master (TLS handshake stall,
# brief FTL wedge, gravity run) doesn't cause a phantom transfer to the
# standby just because it happened to serve a few queries during the gap.
# The candidate must be *processing traffic* (advancing query watermark)
# for all 5 consecutive polls — a standby that briefly answers a query and
# then goes quiet again won't trip the gate.
_VIP_TRANSFER_CONFIRM_POLLS = 5

# Fire-and-forget Pushover notification tasks. asyncio keeps only weak refs
# to bare `create_task(...)` — stash each task here and log any exception so
# that a failed notify doesn't silently vanish.
_background_tasks: set[asyncio.Task] = set()

# Per-instance circuit breaker.  After N consecutive SSL/connection failures
# against one Pi-hole, skip polls for that instance until the cooldown expires.
# The first poll after cooldown is a "probe" — success closes the breaker,
# another failure re-arms it.  Motivated by Pi-hole FTL on slow hardware
# (Raspberry Pi 3) occasionally wedging its TLS session table and rejecting
# every handshake until restarted; hammering it at the normal cadence only
# prolongs the wedge.  Stats and queries share the same breaker state keyed
# by instance id — they hit the same FTL.
# Tunables are sourced from Settings (env-overridable) so operators can
# retune a flap-prone Pi-hole without a rebuild.  Defaults match the hard-coded
# values that preceded 1.8.0-dev.16.
_CIRCUIT_FAIL_THRESHOLD = settings.circuit_fail_threshold
_CIRCUIT_COOLDOWN = timedelta(seconds=settings.circuit_cooldown_seconds)
# Stats and queries polls are scheduled concurrently and hit the same FTL over
# the same persistent TCP/TLS connection.  When that connection goes bad both
# polls fail in the same tick.  Without dedup, each tick would advance the
# counter by 2 and the breaker would trip on the first genuine hiccup (which
# is what made pihole1 flap under dev.10).  Any failures within this window
# of the last counted failure are treated as the same event and don't
# increment.
_CIRCUIT_DEDUP_WINDOW = settings.circuit_dedup_seconds
_consec_failures: dict[str, int] = {}
_cooldown_until: dict[str, datetime] = {}
_last_failure_at: dict[str, float] = {}


class _CircuitOpen(Exception):
    """Raised internally when the per-instance breaker is in cooldown."""


def _breaker_allows(key: str, name: str) -> bool:
    until = _cooldown_until.get(key)
    if until is None:
        return True
    if datetime.now(UTC) >= until:
        logger.info("Circuit breaker probe for %s (cooldown elapsed)", name)
        return True
    return False


def _breaker_success(key: str, name: str) -> None:
    if key in _cooldown_until or _consec_failures.get(key):
        logger.info("Circuit breaker closed for %s after successful poll", name)
    _consec_failures.pop(key, None)
    _cooldown_until.pop(key, None)
    _last_failure_at.pop(key, None)


def _breaker_failure(key: str, name: str) -> None:
    now = time.monotonic()
    last = _last_failure_at.get(key, 0.0)
    if last and now - last < _CIRCUIT_DEDUP_WINDOW:
        # Same underlying failure (stats + queries on the shared connection);
        # update the timestamp but don't double-count against the breaker.
        _last_failure_at[key] = now
        return
    _last_failure_at[key] = now
    count = _consec_failures.get(key, 0) + 1
    _consec_failures[key] = count
    if count >= _CIRCUIT_FAIL_THRESHOLD:
        _cooldown_until[key] = datetime.now(UTC) + _CIRCUIT_COOLDOWN
        logger.warning(
            "Circuit breaker tripped for %s (%d consecutive failures) — "
            "cooling down for %s", name, count, _CIRCUIT_COOLDOWN,
        )


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.exception("collector background task failed", exc_info=exc)

    task.add_done_callback(_done)
