"""Background APScheduler jobs that poll Pi-hole instances."""
from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import datetime, timedelta, timezone

import httpx

from sqlalchemy import delete, func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance, QueryLog, StatsSnapshot
from app.models.user import RevokedToken
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
    except Exception as exc:
        logger.warning("Failed to poll stats for %s: %s", instance.name, exc)
        if isinstance(exc, (ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError)):
            logger.info("Connection error for %s — evicting client for next poll", instance.name)
            await close_client(str(instance.id))
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
        if isinstance(exc, (ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError)):
            logger.info("Connection error for %s — evicting client for next poll", instance.name)
            await close_client(str(instance.id))


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


async def shutdown() -> None:
    """Call on shutdown to cleanly close all persistent HTTP clients."""
    await close_all_clients()
    _last_seen_ts.clear()
    _offline_retry_count.clear()
    _offline_alert_count.clear()
