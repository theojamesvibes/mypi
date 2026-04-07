"""Background APScheduler jobs that poll Pi-hole instances."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance, QueryLog, StatsSnapshot
from app.services.pihole_client import PiholeClient

logger = logging.getLogger(__name__)

_query_cursors: dict[str, str | None] = {}


async def _get_active_instances() -> list[PiholeInstance]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PiholeInstance).where(PiholeInstance.is_active.is_(True)))
        return list(result.scalars().all())


async def _poll_stats_for(instance: PiholeInstance) -> None:
    try:
        async with PiholeClient(instance.url, instance.api_password) as client:
            summary = await client.get_summary()
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


async def poll_stats() -> None:
    instances = await _get_active_instances()
    await asyncio.gather(*[_poll_stats_for(inst) for inst in instances])


async def _poll_queries_for(instance: PiholeInstance) -> None:
    instance_key = str(instance.id)
    cursor = _query_cursors.get(instance_key)
    try:
        async with PiholeClient(instance.url, instance.api_password) as client:
            queries, next_cursor = await client.get_queries(cursor=cursor, length=500)

        if not queries:
            if next_cursor:
                _query_cursors[instance_key] = next_cursor
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
                logger.debug("Stored %d new queries for %s", len(new_logs), instance.name)

        if next_cursor:
            _query_cursors[instance_key] = next_cursor

    except Exception as exc:
        logger.warning("Failed to poll queries for %s: %s", instance.name, exc)


async def poll_queries() -> None:
    instances = await _get_active_instances()

    active_keys = {str(inst.id) for inst in instances}
    for key in list(_query_cursors):
        if key not in active_keys:
            del _query_cursors[key]

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
