"""Background APScheduler jobs that poll Pi-hole instances."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance, QueryLog, StatsSnapshot
from app.services import pihole_version_check
from app.services import pushover as pushover_service
from app.services import sync_service
from app.services.client_manager import close_client, close_all_clients, get_client, save_sid

logger = logging.getLogger(__name__)

# Most recent query timestamp seen per instance (Unix float).
# Used to fetch only queries newer than what we already have.
_last_seen_ts: dict[str, float] = {}

# Previous poll status — used to detect online→offline transitions for alerts.
_prev_status: dict[str, str] = {}

# Consecutive offline polls already retried before the first alert fires.
# Resets on transition (online→offline) and on recovery.
_offline_retry_count: dict[str, int] = {}

# Number of offline alerts already sent per instance in the current outage period.
# Resets to 0 when the instance recovers.  Used to enforce offline_alert_max_count.
_offline_alert_count: dict[str, int] = {}


async def _get_active_instances() -> list[PiholeInstance]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PiholeInstance).where(PiholeInstance.is_active.is_(True)))
        return list(result.scalars().all())


async def _poll_stats_for(instance: PiholeInstance) -> None:
    version_info = None
    try:
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
        try:
            version_info = await client.get_version_info()
        except Exception as exc:
            logger.warning("Could not fetch version info for %s: %s", instance.name, exc)
    except Exception as exc:
        logger.warning("Failed to poll stats for %s: %s", instance.name, exc)
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
                if version_info is not None:
                    inst.version_core = version_info.core.current or None
                    inst.version_ftl = version_info.ftl.current or None
                    inst.version_web = version_info.web.current or None
                    inst.update_available_core = pihole_version_check.compute_update_available(inst.version_core, "core")
                    inst.update_available_ftl = pihole_version_check.compute_update_available(inst.version_ftl, "ftl")
                    inst.update_available_web = pihole_version_check.compute_update_available(inst.version_web, "web")
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
                    asyncio.create_task(pushover_service.notify_instance_offline(instance.name))
    elif snapshot.status == "online" and prev == "offline":
        # Recovery: alert only if we had already sent an offline alert (avoids spurious
        # "back online" pings for blips that resolved before retries were exhausted).
        already_alerted = _offline_alert_count.get(key, 0) > 0
        _offline_retry_count.pop(key, None)
        _offline_alert_count.pop(key, None)
        if already_alerted:
            asyncio.create_task(pushover_service.notify_instance_back_online(instance.name))
    _prev_status[key] = snapshot.status

    # Notify sync service if this is the master (enables auto-gravity detection)
    if instance.is_master and snapshot.status == "online":
        await sync_service.notify_blocklist_count(snapshot.domains_on_blocklist)


async def poll_stats() -> None:
    instances = await _get_active_instances()
    await asyncio.gather(*[_poll_stats_for(inst) for inst in instances])


async def _poll_queries_for(instance: PiholeInstance) -> None:
    instance_key = str(instance.id)
    from_ts = _last_seen_ts.get(instance_key)
    logger.info("Polling queries for %s (from_ts=%s)", instance.name, from_ts)
    try:
        client = await get_client(instance)
        queries = await client.get_queries(from_ts=from_ts, length=500)

        await save_sid(instance.id, client.sid)
        logger.info("Got %d queries from %s", len(queries), instance.name)

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

        # Advance the watermark to the most recent timestamp we've seen.
        max_ts = max(q.timestamp.timestamp() for q in queries)
        _last_seen_ts[instance_key] = max_ts

    except Exception as exc:
        logger.warning("Failed to poll queries for %s: %s", instance.name, exc)


async def poll_queries() -> None:
    instances = await _get_active_instances()

    active_keys = {str(inst.id) for inst in instances}
    for key in list(_last_seen_ts):
        if key not in active_keys:
            del _last_seen_ts[key]
    for key in list(_last_seen_ts):
        if key not in active_keys:
            await close_client(key)

    await asyncio.gather(*[_poll_queries_for(inst) for inst in instances])


async def cleanup_old_data() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_retention_days)
    async with AsyncSessionLocal() as db:
        snap_result = await db.execute(
            delete(StatsSnapshot).where(StatsSnapshot.collected_at < cutoff)
        )
        query_result = await db.execute(
            delete(QueryLog).where(QueryLog.timestamp < cutoff)
        )
        await db.commit()
        logger.info(
            "Cleanup: removed %d snapshots and %d query log entries older than %d days.",
            snap_result.rowcount,
            query_result.rowcount,
            settings.data_retention_days,
        )


async def shutdown() -> None:
    """Call on shutdown to cleanly close all persistent HTTP clients."""
    await close_all_clients()
    _last_seen_ts.clear()
    _offline_retry_count.clear()
    _offline_alert_count.clear()
