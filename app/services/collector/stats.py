"""Stats poll job: per-instance snapshot, offline alerts, stall detection.

`poll_stats_for_site` is the scheduled entry point (one job per active
site). After the per-instance gather it hands advance signals to the VIP
state machine in vip.py.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from datetime import UTC, datetime

import httpx

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance, StatsSnapshot
from app.services import pushover as pushover_service
from app.services import sync_service
from app.services.client_manager import close_client, get_client, save_sid
from app.services.collector import state
from app.services.collector.instances import _get_instances_for_site, _get_site_name
from app.services.collector.state import (
    _STALL_THRESHOLD_POLLS,
    _breaker_allows,
    _breaker_failure,
    _breaker_success,
    _CircuitOpen,
    _last_seen_ts,
    _offline_alert_count,
    _offline_retry_count,
    _prev_dns_queries_today,
    _prev_status,
    _prev_watermark_for_stall,
    _stall_alerted,
    _stall_count,
    _vip_lead_streak,
    _vip_prev_count,
)
from app.services.collector.vip import _check_vip_state

logger = logging.getLogger(__name__)


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
            state._spawn(pushover_service.notify_instance_recovered_from_stall(
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
        state._spawn(pushover_service.notify_instance_stalled(
            instance.name, site_name=site_name, site_id=instance.site_id,
        ))


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
    # 1. Poll Pi-hole and build the stats snapshot (offline snapshot on failure).
    key = str(instance.id)
    try:
        if not _breaker_allows(key, instance.name):
            raise _CircuitOpen
        client = await get_client(instance)
        summary = await client.get_summary()
        await save_sid(instance.id, client.sid)
        snapshot = StatsSnapshot(
            instance_id=instance.id,
            collected_at=datetime.now(UTC),
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
            collected_at=datetime.now(UTC),
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
            collected_at=datetime.now(UTC),
            status="offline",
        )

    # 2. Persist the snapshot (and bump last_seen_at when online).
    async with AsyncSessionLocal() as db:
        db.add(snapshot)
        if snapshot.status == "online":
            inst = await db.get(PiholeInstance, instance.id)
            if inst:
                inst.last_seen_at = snapshot.collected_at
        await db.commit()

    # 3. Offline-alert bookkeeping.
    # Pushover alerts: retry-then-alert + transition-based + configurable repeat for sustained outages.
    # Offline-alert policy, in plain terms:
    #   1. First poll ever for an instance → just record its status, never alert.
    #   2. Goes offline → wait N polls first (a brief blip shouldn't page anyone).
    #   3. Still offline after N polls → send an alert, then send at most
    #      `max_count` more per outage (0 = alert every poll) to avoid spamming.
    #   4. Comes back online → send ONE "recovered" alert, but only if we had
    #      actually alerted about the outage.
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
                    state._spawn(pushover_service.notify_instance_offline(
                        instance.name, site_name=site_name, site_id=instance.site_id,
                    ))
    elif snapshot.status == "online" and prev == "offline":
        # Recovery: alert only if we had already sent an offline alert (avoids spurious
        # "back online" pings for blips that resolved before retries were exhausted).
        already_alerted = _offline_alert_count.get(key, 0) > 0
        _offline_retry_count.pop(key, None)
        _offline_alert_count.pop(key, None)
        if already_alerted:
            state._spawn(pushover_service.notify_instance_back_online(
                instance.name, site_name=site_name, site_id=instance.site_id,
            ))
    _prev_status[key] = snapshot.status

    # 4. Stall / VIP advance signal.
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
        # An offline VIP node also resets its lead streak and query-count
        # baseline, so a brief outage on the master doesn't leave stale
        # state that causes a phantom transfer (or a spurious huge delta)
        # the moment it reappears.
        _vip_lead_streak.pop(key, None)
        _vip_prev_count.pop(key, None)

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
    for inst, res in zip(instances, results, strict=False):
        if isinstance(res, BaseException):
            logger.warning(
                "_poll_stats_for(%s) raised: %s: %s",
                inst.name, type(res).__name__, res,
            )
            continue
        snapshot, advanced = res
        poll_outcomes.append((inst, snapshot, advanced))

    # Count how many instances SHOULD be in the VIP cluster (per config), not
    # how many answered — the group-stall check needs the full expected size to
    # tell "all nodes flat" from "some nodes simply didn't respond this poll".
    configured_vip_count = sum(
        1 for inst in instances if inst.vip_role in ("master", "replica")
    )
    if configured_vip_count:
        await _check_vip_state(
            site_id, site_name, poll_outcomes, configured_vip_count,
        )
