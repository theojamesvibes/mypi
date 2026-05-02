"""Background APScheduler jobs that poll Pi-hole instances.

Scheduler structure (Phase 3 onward):
  For each active site, a pair of APScheduler jobs — `poll_stats_site_<id>`
  and `poll_queries_site_<id>` — polls only that site's instances. Adding
  or removing a site updates just its pair via `schedule_site` /
  `unschedule_site`; a poll-interval change reschedules every site's
  queries job via `reschedule_all_queries_jobs`. Sites are independent:
  a hang on one site's poll doesn't delay another's next tick.

  Per-instance module state (watermarks, offline counters, circuit
  breaker) is still keyed by instance id — site scoping doesn't change
  what we track, only which instances we touch per tick. A dedicated
  `prune_inactive_state` job clears dict entries for instances that
  config_loader has deactivated.

  The master-blocklist-delta auto-sync trigger only fires for the Main
  site's master in Phase 3. `sync_service` still has global
  single-master state; Phase 4 rewrites it to be per-site and this
  guard goes away.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from sqlalchemy import delete, func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance, QueryLog, StatsSnapshot
from app.models.site import Site
from app.models.user import RevokedToken
from app.services import pihole_version_check
from app.services import pushover as pushover_service
from app.services import query_stream
from app.services import sync_service
from app.services.client_manager import close_client, close_all_clients, get_client, save_sid

logger = logging.getLogger(__name__)

# Most recent query timestamp seen per instance (Unix float).
# Used to fetch only queries newer than what we already have.
_last_seen_ts: dict[str, float] = {}

# Previous poll status — used to detect online→offline transitions for alerts.
# Pruned alongside `_last_seen_ts` when an instance is removed (see
# `poll_queries`), so this doesn't accumulate entries for deactivated
# instances.
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
    if datetime.now(timezone.utc) >= until:
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
        _cooldown_until[key] = datetime.now(timezone.utc) + _CIRCUIT_COOLDOWN
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


async def _get_active_instances() -> list[PiholeInstance]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PiholeInstance).where(PiholeInstance.is_active.is_(True)))
        return list(result.scalars().all())


async def _get_instances_for_site(site_id: uuid.UUID) -> list[PiholeInstance]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PiholeInstance).where(
                PiholeInstance.site_id == site_id,
                PiholeInstance.is_active.is_(True),
            )
        )
        return list(result.scalars().all())


async def get_active_site_ids() -> list[uuid.UUID]:
    """Used by startup to register a poll-job pair per active site."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Site.id).where(Site.is_active.is_(True)).order_by(Site.sort_order, Site.name)
        )
        return [row[0] for row in result.fetchall()]


async def _get_site_name(site_id: uuid.UUID) -> str:
    """Look up a site's human-readable name for alert/log messages.
    Returns the stringified id if the site is missing (shouldn't happen;
    defensive fallback)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Site.name).where(Site.id == site_id))
        name = result.scalar_one_or_none()
    return name or str(site_id)


def _instance_advanced(
    instance: PiholeInstance, snapshot: StatsSnapshot,
) -> bool | None:
    """Update the per-instance dns_queries_today / watermark baselines and
    return True if either advanced this poll, False if both were flat.
    Returns None on the bootstrap poll (no prior baseline).

    Centralizes the "did this instance see traffic this poll?" question
    used by both the per-instance stall detector and the per-site VIP
    check. Midnight rollover (counter reset) clears any pending stall
    state and is treated as a non-advance (caller decides what to do).
    """
    key = str(instance.id)
    new_count = snapshot.dns_queries_today or 0
    prev_count = _prev_dns_queries_today.get(key)
    new_watermark = _last_seen_ts.get(key)
    prev_watermark = _prev_watermark_for_stall.get(key)

    if prev_count is None:
        _prev_dns_queries_today[key] = new_count
        _prev_watermark_for_stall[key] = new_watermark
        return None

    if new_count < prev_count:
        # Midnight rollover or FTL restart. Treat as reset.
        _prev_dns_queries_today[key] = new_count
        _prev_watermark_for_stall[key] = new_watermark
        _stall_count.pop(key, None)
        if _stall_alerted.pop(key, None):
            logger.info(
                "Stall state cleared for %s due to counter rollover.",
                instance.name,
            )
        return False

    counter_advanced = new_count > prev_count
    watermark_advanced = (
        new_watermark is not None
        and (prev_watermark is None or new_watermark > prev_watermark)
    )

    _prev_dns_queries_today[key] = new_count
    _prev_watermark_for_stall[key] = new_watermark
    return counter_advanced or watermark_advanced


def _check_stalled(
    instance: PiholeInstance, advanced: bool | None, site_name: str,
) -> None:
    """Per-instance stall detector for non-VIP instances.

    Two independent signals must both be flat for `_STALL_THRESHOLD_POLLS`
    polls before we alert: the cumulative `dns_queries_today` counter
    and the `/api/queries` watermark. Requiring both reduces false
    positives on legitimate idle moments. Skipped for VIP-grouped
    instances (vip_role is not None) — idle is normal on a standby and a
    cluster-level check in `_check_vip_state` replaces it.
    """
    if instance.vip_role is not None:
        return  # VIP-aware path handles this in _check_vip_state.
    if advanced is None:
        return  # Bootstrap poll — no prior baseline yet.

    key = str(instance.id)

    if advanced:
        _stall_count.pop(key, None)
        if _stall_alerted.pop(key, None):
            logger.info(
                "Instance %s recovered from stalled state.", instance.name,
            )
            _spawn(pushover_service.notify_instance_recovered_from_stall(
                instance.name, site_name=site_name, site_id=instance.site_id,
            ))
        return

    stalls = _stall_count.get(key, 0) + 1
    _stall_count[key] = stalls

    if stalls == _STALL_THRESHOLD_POLLS and not _stall_alerted.get(key):
        _stall_alerted[key] = True
        logger.error(
            "Instance %s appears stalled — admin API responsive but "
            "dns_queries_today and query watermark have not advanced for "
            "%d consecutive polls (~%d minutes). Likely FTL split-state; "
            "consider `systemctl restart pihole-FTL` on the host.",
            instance.name, stalls, stalls,
        )
        _spawn(pushover_service.notify_instance_stalled(
            instance.name, site_name=site_name, site_id=instance.site_id,
        ))


async def _check_vip_state(
    site_id: uuid.UUID,
    site_name: str,
    poll_outcomes: list[tuple[PiholeInstance, StatsSnapshot, bool | None]],
    configured_vip_count: int,
) -> None:
    """Per-site VIP cluster detector.

    Runs after every site stats poll. Looks at the subset of instances in
    `poll_outcomes` whose `vip_role` is "master" or "replica" and:

      1. Maintains `_vip_last_advance_seq` for each VIP node.
      2. Identifies the "currently active" node (most-recently-advanced).
      3. Fires `notify_vip_transfer` when the active node changes, gated
         by `_VIP_TRANSFER_CONFIRM_POLLS` consecutive advancing polls on
         the candidate so a single transient doesn't bounce the active
         label.
      4. Fires `notify_vip_group_stalled` when every node in the cluster
         has gone flat for `_STALL_THRESHOLD_POLLS` polls (the whole VIP
         is dead). Recovery clears the alert; a future flat run can fire
         again.

    Bootstrap: a cluster that has *never* seen any node advance since
    process start can't distinguish "fresh install with no traffic yet"
    from "real outage." We require at least one observed advance from
    any node in the cluster before the group-stall alert can fire.
    Truly-dead instances are still caught by the existing offline check.
    """
    vip_outcomes = [
        (inst, snap, adv) for inst, snap, adv in poll_outcomes
        if inst.vip_role in ("master", "replica") and snap.status == "online"
    ]
    if not vip_outcomes:
        return

    seq = _site_poll_seq.get(site_id, 0) + 1
    _site_poll_seq[site_id] = seq

    # Update per-instance advance bookkeeping.
    any_advanced_this_poll = False
    ever_advanced = False
    for inst, _snap, advanced in vip_outcomes:
        key = str(inst.id)
        if advanced:
            _vip_last_advance_seq[key] = seq
            _vip_advance_streak[key] = _vip_advance_streak.get(key, 0) + 1
            any_advanced_this_poll = True
        elif advanced is False:
            _vip_advance_streak[key] = 0
        # advanced is None on bootstrap → don't update streak yet.
        if key in _vip_last_advance_seq:
            ever_advanced = True

    # Seed the active node on the very first poll where we can determine
    # one. Prefer the configured vip_master so a fresh start doesn't
    # mis-label whichever replica happens to advance first.
    if site_id not in _vip_active_node:
        configured_master = next(
            (inst for inst, _, _ in vip_outcomes if inst.vip_role == "master"),
            None,
        )
        _vip_active_node[site_id] = (
            configured_master.id if configured_master else None
        )

    # Transfer detection — pick the node with the highest streak that
    # also has the most recent advance. If the current active node also
    # advanced this poll, it stays put; only when *another* node has
    # been advancing for `_VIP_TRANSFER_CONFIRM_POLLS` polls in a row
    # while the current active has gone idle do we declare a transfer.
    current_active_id = _vip_active_node.get(site_id)
    current_active_inst = next(
        (inst for inst, _, _ in vip_outcomes if inst.id == current_active_id),
        None,
    )
    current_active_idle = (
        current_active_inst is None
        or _vip_advance_streak.get(str(current_active_inst.id), 0) == 0
    )

    if current_active_idle and any_advanced_this_poll:
        # Look for a confirmed challenger.
        challenger: PiholeInstance | None = None
        for inst, _snap, _adv in vip_outcomes:
            if inst.id == current_active_id:
                continue
            streak = _vip_advance_streak.get(str(inst.id), 0)
            if streak >= _VIP_TRANSFER_CONFIRM_POLLS:
                # Prefer the configured vip_master if multiple nodes
                # qualify — closest match to "the cluster's normal state."
                if challenger is None or inst.vip_role == "master":
                    challenger = inst

        if challenger is not None and challenger.id != current_active_id:
            old_name = (
                current_active_inst.name if current_active_inst else "unknown"
            )
            logger.warning(
                "VIP transfer in site '%s': active node %s → %s "
                "(streak=%d).",
                site_name, old_name, challenger.name,
                _vip_advance_streak.get(str(challenger.id), 0),
            )
            _vip_active_node[site_id] = challenger.id
            _spawn(pushover_service.notify_vip_transfer(
                old_name=old_name,
                new_name=challenger.name,
                site_name=site_name,
                site_id=site_id,
            ))

    # Group stall — every VIP node has been flat for >= threshold polls
    # AND the cluster has at least one historical advance to anchor
    # against (so a fresh install with no traffic doesn't fire).
    # Require positive observation of every configured VIP node this poll:
    # an offline/missing node returns no signal, and treating that as "flat"
    # combined with a quiet standby produced false alerts on transient
    # master TLS blips. Per-instance offline alerts cover real outages.
    all_observed_online = len(vip_outcomes) == configured_vip_count
    if ever_advanced and all_observed_online:
        all_flat = all(
            seq - _vip_last_advance_seq.get(str(inst.id), seq)
            >= _STALL_THRESHOLD_POLLS
            for inst, _snap, _adv in vip_outcomes
        )
        if all_flat and not _vip_group_stall_alerted.get(site_id):
            _vip_group_stall_alerted[site_id] = True
            names = ", ".join(inst.name for inst, _, _ in vip_outcomes)
            logger.error(
                "VIP cluster stalled in site '%s' — every node (%s) has "
                "been flat for >= %d polls. The whole VIP appears dead.",
                site_name, names, _STALL_THRESHOLD_POLLS,
            )
            _spawn(pushover_service.notify_vip_group_stalled(
                names=names, site_name=site_name, site_id=site_id,
            ))
        elif any_advanced_this_poll and _vip_group_stall_alerted.get(site_id):
            _vip_group_stall_alerted[site_id] = False
            logger.info("VIP cluster in site '%s' recovered.", site_name)
            _spawn(pushover_service.notify_vip_group_recovered(
                site_name=site_name, site_id=site_id,
            ))


# Tracks the watermark seen at the moment of the last stats poll, so the
# stall check compares "watermark now" vs "watermark last poll" rather than
# "watermark now" vs "earliest watermark ever". A poll that actually advances
# the watermark therefore clears stall state on the very next poll, even if
# the instance was previously busy and `_last_seen_ts` was already populated.
_prev_watermark_for_stall: dict[str, float | None] = {}


async def _poll_stats_for(
    instance: PiholeInstance, site_name: str = "",
) -> tuple[StatsSnapshot, bool | None]:
    """Poll stats for one instance, persist a snapshot, and run alert
    bookkeeping (online/offline, per-instance stall for non-VIP).

    Returns `(snapshot, advanced)` so the per-site VIP check can correlate
    advance signals across the cluster after every site poll completes.
    `advanced` is True/False on a normal poll, None on the bootstrap poll
    or on midnight rollover.
    """
    key = str(instance.id)
    try:
        if not _breaker_allows(key, instance.name):
            raise _CircuitOpen
        client = await get_client(instance)
        summary = await client.get_summary()
        await save_sid(instance.id, client.sid)
        snapshot = StatsSnapshot(
            instance_id=instance.id,
            collected_at=datetime.now(timezone.utc),
            status="online",
            dns_queries_today=summary.dns_queries_today,
            queries_blocked=summary.queries_blocked,
            percent_blocked=summary.percent_blocked,
            domains_on_blocklist=summary.domains_on_blocklist,
            unique_clients=summary.unique_clients,
            queries_cached=summary.queries_cached,
            queries_forwarded=summary.queries_forwarded,
        )
        _breaker_success(key, instance.name)
    except _CircuitOpen:
        snapshot = StatsSnapshot(
            instance_id=instance.id,
            collected_at=datetime.now(timezone.utc),
            status="offline",
        )
    except Exception as exc:
        logger.warning("Failed to poll stats for %s: %s: %s", instance.name, type(exc).__name__, exc)
        if isinstance(exc, (ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError)):
            logger.info("Connection error for %s — evicting client for next poll", instance.name)
            await close_client(key)
            _breaker_failure(key, instance.name)
        snapshot = StatsSnapshot(
            instance_id=instance.id,
            collected_at=datetime.now(timezone.utc),
            status="offline",
        )

    async with AsyncSessionLocal() as db:
        db.add(snapshot)
        if snapshot.status == "online":
            inst = await db.get(PiholeInstance, instance.id)
            if inst:
                inst.last_seen_at = snapshot.collected_at
        await db.commit()

    # Pushover alerts: retry-then-alert + transition-based + configurable repeat for sustained outages
    key = str(instance.id)
    prev = _prev_status.get(key)
    if prev is None:
        # First poll ever — establish baseline; no alert regardless of status.
        if snapshot.status == "offline":
            _offline_retry_count[key] = 0
            _offline_alert_count[key] = 0
    elif snapshot.status == "offline":
        required_retries = pushover_service.get_offline_alert_retries()
        if prev != "offline":
            # Transition online→offline: start the retry countdown; do not alert yet.
            _offline_retry_count[key] = 0
            _offline_alert_count[key] = 0
        else:
            retries_done = _offline_retry_count.get(key, 0)
            if retries_done < required_retries:
                # Still within the retry window — wait another poll.
                _offline_retry_count[key] = retries_done + 1
            else:
                # Retries exhausted: alert, then respect offline_alert_max_count for repeats.
                # 0 = alert every poll; 1-10 = alert at most N times per outage.
                max_count = pushover_service.get_offline_alert_max_count()
                current = _offline_alert_count.get(key, 0)
                if current == 0 or max_count == 0 or current < max_count:
                    _offline_alert_count[key] = current + 1
                    _spawn(pushover_service.notify_instance_offline(
                        instance.name, site_name=site_name, site_id=instance.site_id,
                    ))
    elif snapshot.status == "online" and prev == "offline":
        # Recovery: alert only if we had already sent an offline alert (avoids spurious
        # "back online" pings for blips that resolved before retries were exhausted).
        already_alerted = _offline_alert_count.get(key, 0) > 0
        _offline_retry_count.pop(key, None)
        _offline_alert_count.pop(key, None)
        if already_alerted:
            _spawn(pushover_service.notify_instance_back_online(
                instance.name, site_name=site_name, site_id=instance.site_id,
            ))
    _prev_status[key] = snapshot.status

    # Stall / VIP detection — only meaningful when the stats poll succeeded.
    # Compute the advance signal once and hand it to both the per-instance
    # stall path (skipped for VIP-grouped instances) and the per-site VIP
    # path (called from `poll_stats_for_site` after the gather).
    advanced: bool | None
    if snapshot.status == "online":
        advanced = _instance_advanced(instance, snapshot)
        _check_stalled(instance, advanced, site_name)
    else:
        advanced = None
        _prev_dns_queries_today.pop(key, None)
        _stall_count.pop(key, None)
        _stall_alerted.pop(key, None)
        # An offline VIP node also resets its advance streak so a brief
        # outage on the master doesn't leave a stale streak that causes a
        # phantom transfer the moment it reappears.
        _vip_advance_streak.pop(key, None)

    # Notify sync service if this is the master (enables auto-gravity detection).
    # Per-site as of Phase 4b — each site's master has its own blocklist-count
    # watermark, so multiple sites' masters can trigger their own auto-syncs
    # independently without thrashing shared state.
    if instance.is_master and snapshot.status == "online":
        await sync_service.notify_blocklist_count(instance.site_id, snapshot.domains_on_blocklist)

    return snapshot, advanced


async def poll_stats_for_site(site_id: uuid.UUID) -> None:
    """Poll stats for every active instance in one site."""
    instances = await _get_instances_for_site(site_id)
    if not instances:
        return
    site_name = await _get_site_name(site_id)
    results = await asyncio.gather(
        *[_poll_stats_for(inst, site_name=site_name) for inst in instances],
        return_exceptions=True,
    )
    # Re-pair results with their instance and filter out exceptions so a
    # single instance crashing the poll doesn't break VIP bookkeeping for
    # the rest of the cluster.
    poll_outcomes: list[tuple[PiholeInstance, StatsSnapshot, bool | None]] = []
    for inst, res in zip(instances, results):
        if isinstance(res, BaseException):
            logger.warning(
                "_poll_stats_for(%s) raised: %s: %s",
                inst.name, type(res).__name__, res,
            )
            continue
        snapshot, advanced = res
        poll_outcomes.append((inst, snapshot, advanced))

    configured_vip_count = sum(
        1 for inst in instances if inst.vip_role in ("master", "replica")
    )
    if configured_vip_count:
        await _check_vip_state(
            site_id, site_name, poll_outcomes, configured_vip_count,
        )


async def _fetch_version_for(instance: PiholeInstance) -> None:
    """Fetch and persist version info for a single instance (no stats, no alerts)."""
    try:
        client = await get_client(instance)
        version_info = await client.get_version_info()
        await save_sid(instance.id, client.sid)
        async with AsyncSessionLocal() as db:
            inst = await db.get(PiholeInstance, instance.id)
            if inst:
                inst.version_core = version_info.core.current or None
                inst.version_ftl = version_info.ftl.current or None
                inst.version_web = version_info.web.current or None
                inst.update_available_core = pihole_version_check.compute_update_available(inst.version_core, "core")
                inst.update_available_ftl = pihole_version_check.compute_update_available(inst.version_ftl, "ftl")
                inst.update_available_web = pihole_version_check.compute_update_available(inst.version_web, "web")
                await db.commit()
        logger.info("Fetched version info for %s: core=%s ftl=%s web=%s",
                    instance.name, version_info.core.current, version_info.ftl.current, version_info.web.current)
    except Exception as exc:
        logger.warning("Could not fetch version info for %s: %s", instance.name, exc)


async def fetch_all_instance_versions() -> None:
    """Fetch and persist Pi-hole version info for all active instances.

    Lightweight — no stats snapshot, no query poll, no alerts.
    Called at startup and after each sync so version data is always fresh.
    """
    instances = await _get_active_instances()
    await asyncio.gather(*[_fetch_version_for(inst) for inst in instances])


async def _poll_queries_for(instance: PiholeInstance) -> None:
    instance_key = str(instance.id)
    from_ts = _last_seen_ts.get(instance_key)
    logger.info("Polling queries for %s (from_ts=%s)", instance.name, from_ts)
    try:
        if not _breaker_allows(instance_key, instance.name):
            return
        client = await get_client(instance)
        queries = await client.get_queries(from_ts=from_ts, length=500)

        await save_sid(instance.id, client.sid)
        logger.info("Got %d queries from %s", len(queries), instance.name)
        _breaker_success(instance_key, instance.name)

        if not queries:
            return

        async with AsyncSessionLocal() as db:
            pihole_ids = [q.pihole_id for q in queries if q.pihole_id]
            existing_ids: set[str] = set()
            if pihole_ids:
                result = await db.execute(
                    select(QueryLog.pihole_query_id).where(
                        QueryLog.instance_id == instance.id,
                        QueryLog.pihole_query_id.in_(pihole_ids),
                    )
                )
                existing_ids = {row[0] for row in result.fetchall()}

            new_logs = [
                QueryLog(
                    instance_id=instance.id,
                    pihole_query_id=q.pihole_id,
                    timestamp=q.timestamp,
                    client_ip=q.client_ip,
                    client_name=q.client_name,
                    query_type=q.query_type,
                    domain=q.domain,
                    status=q.status,
                    reply_type=q.reply_type,
                    reply_time_ms=q.reply_time_ms,
                )
                for q in queries
                if not (q.pihole_id and q.pihole_id in existing_ids)
            ]
            if new_logs:
                db.add_all(new_logs)
                await db.commit()
                logger.info("Stored %d new queries for %s", len(new_logs), instance.name)
                # Wake any /api/queries/stream subscribers so the Live view
                # refetches only when there's actually something new.
                query_stream.publish(instance.id, instance.site_id, len(new_logs))

        # Advance the watermark to the most recent timestamp we've seen.
        max_ts = max(q.timestamp.timestamp() for q in queries)
        _last_seen_ts[instance_key] = max_ts

    except Exception as exc:
        logger.warning("Failed to poll queries for %s: %s: %s", instance.name, type(exc).__name__, exc)
        if isinstance(exc, (ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError)):
            logger.info("Connection error for %s — evicting client for next poll", instance.name)
            await close_client(instance_key)
            _breaker_failure(instance_key, instance.name)


async def poll_queries_for_site(site_id: uuid.UUID) -> None:
    """Poll queries for every active instance in one site.

    Per-instance state cleanup for globally-deactivated instances lives in
    the dedicated `prune_inactive_state` job — running it in every site's
    poll would either redundantly hit the DB or risk cross-site races.
    """
    instances = await _get_instances_for_site(site_id)
    if not instances:
        return
    await asyncio.gather(*[_poll_queries_for(inst) for inst in instances])


async def prune_inactive_state() -> None:
    """Drop per-instance module state for instances that config_loader has
    deactivated, and evict any leftover clients for them.

    config_loader.sync_sites_and_instances already evicts clients when it
    deactivates an instance, so this is belt-and-suspenders. Runs on its
    own infrequent schedule (default every 5 minutes) so state doesn't
    leak between YAML reloads and container restarts.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PiholeInstance.id).where(PiholeInstance.is_active.is_(True))
        )
        active_keys = {str(row[0]) for row in result.fetchall()}

    stale_keys: set[str] = set()
    for state_dict in (
        _last_seen_ts, _prev_status, _offline_retry_count, _offline_alert_count,
        _consec_failures, _cooldown_until, _last_failure_at,
        _prev_dns_queries_today, _prev_watermark_for_stall,
        _stall_count, _stall_alerted,
        _vip_last_advance_seq, _vip_advance_streak,
    ):
        for key in list(state_dict):
            if key not in active_keys:
                stale_keys.add(key)
                del state_dict[key]

    # Site-keyed VIP state: drop entries for sites whose only VIP-flagged
    # instances are all gone (they may have been demoted via YAML edit).
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PiholeInstance.site_id).where(
                PiholeInstance.is_active.is_(True),
                PiholeInstance.vip_role.is_not(None),
            )
        )
        active_vip_site_ids = {row[0] for row in result.fetchall()}
    for site_state in (_vip_active_node, _vip_group_stall_alerted, _site_poll_seq):
        for site_key in list(site_state):
            if site_key not in active_vip_site_ids:
                del site_state[site_key]

    for key in stale_keys:
        try:
            await close_client(key, logout=True)
        except Exception as exc:
            logger.warning("prune_inactive_state: close_client(%s) failed: %s", key, exc)


async def _store_queries(instance: PiholeInstance, queries: list) -> int:
    """Insert queries that are not already in the database. Returns count stored."""
    async with AsyncSessionLocal() as db:
        pihole_ids = [q.pihole_id for q in queries if q.pihole_id]
        existing_ids: set[str] = set()
        if pihole_ids:
            result = await db.execute(
                select(QueryLog.pihole_query_id).where(
                    QueryLog.instance_id == instance.id,
                    QueryLog.pihole_query_id.in_(pihole_ids),
                )
            )
            existing_ids = {row[0] for row in result.fetchall()}

        new_logs = [
            QueryLog(
                instance_id=instance.id,
                pihole_query_id=q.pihole_id,
                timestamp=q.timestamp,
                client_ip=q.client_ip,
                client_name=q.client_name,
                query_type=q.query_type,
                domain=q.domain,
                status=q.status,
                reply_type=q.reply_type,
                reply_time_ms=q.reply_time_ms,
            )
            for q in queries
            if not (q.pihole_id and q.pihole_id in existing_ids)
        ]
        if new_logs:
            db.add_all(new_logs)
            await db.commit()
            # Same wake-the-Live-view publish as the live poll path.
            # Backfill commits historical rows the user's table is unlikely
            # to be paginated to anyway, so the extra refetch is cheap.
            query_stream.publish(instance.id, instance.site_id, len(new_logs))
        return len(new_logs)


async def backfill_queries_for(instance: PiholeInstance, hours: int = 24) -> None:
    """Backfill query history from Pi-hole for any gap since the last stored entry.

    On a healthy DB the most-recent row will be only seconds old, so backfill
    is skipped entirely.  After a TRUNCATE or on first startup the full `hours`
    window is fetched.  In between (e.g. a long container downtime) only the
    gap from the last stored timestamp forward is fetched.

    Fetches one clock hour at a time so the per-request length cap is never
    the bottleneck.  Within each window, pages forward by timestamp if a full
    10 000-row page is returned.
    """
    now = datetime.now(timezone.utc)
    recent_threshold = timedelta(minutes=10)

    # Check the most recent stored query for this instance.
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.max(QueryLog.timestamp)).where(QueryLog.instance_id == instance.id)
        )
        latest_ts: datetime | None = result.scalar_one_or_none()

    if latest_ts is not None and latest_ts.tzinfo is None:
        latest_ts = latest_ts.replace(tzinfo=timezone.utc)

    if latest_ts is not None and (now - latest_ts) < recent_threshold:
        logger.debug("Backfill skipped for %s — data is current (latest: %s)", instance.name, latest_ts)
        return

    # Determine the start of the backfill window.
    if latest_ts is not None:
        backfill_start = latest_ts
        logger.info("Backfill for %s from last stored timestamp %s", instance.name, latest_ts)
    else:
        backfill_start = now - timedelta(hours=hours)
        logger.info("Backfill for %s — no existing data, fetching last %dh", instance.name, hours)

    # Build clock-aligned hourly windows from backfill_start to now.
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    windows: list[tuple[datetime, datetime]] = []
    w = backfill_start.replace(minute=0, second=0, microsecond=0)
    while w < current_hour_start:
        w_end = w + timedelta(hours=1)
        windows.append((max(w, backfill_start), w_end))
        w = w_end
    windows.append((max(current_hour_start, backfill_start), now))

    total_stored = 0
    logger.info("Starting backfill for %s (%d windows)", instance.name, len(windows))
    try:
        client = await get_client(instance)
        for w_start, w_end in windows:
            from_ts  = w_start.timestamp()
            until_ts = w_end.timestamp()
            max_pages = 20  # safety cap per window (~200k queries/hour — far beyond realistic)

            for _ in range(max_pages):
                queries = await client.get_queries(from_ts=from_ts, until_ts=until_ts, length=10000)
                await save_sid(instance.id, client.sid)

                if not queries:
                    break

                stored = await _store_queries(instance, queries)
                total_stored += stored

                if len(queries) < 10000:
                    # Partial page — window exhausted
                    break

                # Full page returned: advance watermark within the window and continue
                max_ts = max(q.timestamp.timestamp() for q in queries)
                if max_ts <= from_ts:
                    break  # no progress guard
                from_ts = max_ts

    except Exception as exc:
        logger.warning("Backfill failed for %s: %s", instance.name, exc)
        return

    logger.info("Backfill complete for %s: stored %d queries", instance.name, total_stored)


async def backfill_all_instances(hours: int = 24) -> None:
    """Run query backfill for all active instances in parallel."""
    instances = await _get_active_instances()
    await asyncio.gather(*[backfill_queries_for(inst, hours=hours) for inst in instances])


async def cleanup_old_data() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_retention_days)
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        snap_result = await db.execute(
            delete(StatsSnapshot).where(StatsSnapshot.collected_at < cutoff)
        )
        query_result = await db.execute(
            delete(QueryLog).where(QueryLog.timestamp < cutoff)
        )
        token_result = await db.execute(
            delete(RevokedToken).where(RevokedToken.expires_at < now)
        )
        await db.commit()
        logger.info(
            "Cleanup: removed %d snapshots, %d query log entries older than %d days, %d expired revoked tokens.",
            snap_result.rowcount,
            query_result.rowcount,
            settings.data_retention_days,
            token_result.rowcount,
        )


def _site_job_id(site_id: uuid.UUID, kind: str) -> str:
    """Job id for a site's poll pair. `kind` is 'stats' or 'queries'."""
    return f"poll_{kind}_site_{site_id}"


def schedule_site(
    scheduler,
    site_id: uuid.UUID,
    stats_interval_seconds: int,
    queries_interval_seconds: int,
) -> None:
    """Register (or replace) the stats+queries poll pair for a site."""
    scheduler.add_job(
        poll_stats_for_site,
        "interval",
        seconds=stats_interval_seconds,
        args=[site_id],
        id=_site_job_id(site_id, "stats"),
        replace_existing=True,
    )
    scheduler.add_job(
        poll_queries_for_site,
        "interval",
        seconds=queries_interval_seconds,
        args=[site_id],
        id=_site_job_id(site_id, "queries"),
        replace_existing=True,
    )
    logger.info(
        "Scheduled site %s (stats=%ds, queries=%ds)",
        site_id, stats_interval_seconds, queries_interval_seconds,
    )


def unschedule_site(scheduler, site_id: uuid.UUID) -> None:
    """Remove the stats+queries poll pair for a site. No-op if not registered."""
    removed = 0
    for kind in ("stats", "queries"):
        job_id = _site_job_id(site_id, kind)
        try:
            scheduler.remove_job(job_id)
            removed += 1
        except Exception:
            pass
    if removed:
        logger.info("Unscheduled site %s (removed %d job(s))", site_id, removed)


def reschedule_all_queries_jobs(scheduler, new_interval_seconds: int) -> None:
    """Apply a new queries-poll interval to every registered site's queries job.

    Called from `poll_settings.set_reschedule_callback` when an operator
    changes the global query-poll interval. Phase 4 will add per-site
    interval overrides via site_settings; until then, the interval is
    applied uniformly.
    """
    rescheduled = 0
    for job in scheduler.get_jobs():
        if job.id.startswith("poll_queries_site_"):
            scheduler.reschedule_job(job.id, trigger="interval", seconds=new_interval_seconds)
            rescheduled += 1
    logger.info(
        "Rescheduled %d site queries-poll job(s) to %ds interval",
        rescheduled, new_interval_seconds,
    )


async def shutdown() -> None:
    """Call on shutdown to cleanly close all persistent HTTP clients."""
    await close_all_clients()
    _last_seen_ts.clear()
    _offline_retry_count.clear()
    _offline_alert_count.clear()
    _consec_failures.clear()
    _cooldown_until.clear()
    _last_failure_at.clear()
    _prev_dns_queries_today.clear()
    _prev_watermark_for_stall.clear()
    _stall_count.clear()
    _stall_alerted.clear()
    _vip_last_advance_seq.clear()
    _vip_advance_streak.clear()
    _vip_active_node.clear()
    _vip_group_stall_alerted.clear()
    _site_poll_seq.clear()
