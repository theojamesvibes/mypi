"""Query-log poll and backfill jobs.

`poll_queries_for_site` is the scheduled entry point (one job per active
site); `backfill_all_instances` runs at startup to close any gap since the
last stored row. Both funnel rows through `_store_queries`, which dedups
on pihole_query_id and publishes a Live-view tick per commit.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance, QueryLog
from app.services import query_stream
from app.services.client_manager import close_client, get_client, save_sid
from app.services.collector.instances import _get_active_instances, _get_instances_for_site
from app.services.collector.state import (
    _breaker_allows,
    _breaker_failure,
    _breaker_success,
    _last_seen_ts,
)

logger = logging.getLogger(__name__)

# Live query polls fetch pages of this size, newest-first (Pi-hole's
# /api/queries truncates at `length` from the newest end). When a page comes
# back full, older rows remain in the window — they're fetched by stepping
# `until` back to the oldest timestamp of each full page. Bounded per tick:
# beyond MAX_PAGES × PAGE_SIZE rows the tick logs a warning and stops, so a
# hot log can't wedge the scheduler; a backfill can recover the gap.
_QUERY_POLL_PAGE_SIZE = 500
_QUERY_POLL_MAX_PAGES = 10


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
                list_id=q.list_id,
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


async def _poll_queries_for(instance: PiholeInstance) -> None:
    """Fetch new query-log rows for one instance since its watermark.

    Pi-hole returns queries newest-first in pages of up to 500. We walk
    backwards page by page (moving the `until` cutoff to the oldest row of
    each full page) until we hit a partial page (the window is drained) or
    the 10-page safety cap (a traffic burst too big for one tick — a later
    backfill recovers it). Finally we advance the watermark to the newest
    timestamp seen so the next poll starts from there.
    """
    instance_key = str(instance.id)
    from_ts = _last_seen_ts.get(instance_key)
    logger.info("Polling queries for %s (from_ts=%s)", instance.name, from_ts)
    try:
        if not _breaker_allows(instance_key, instance.name):
            return
        client = await get_client(instance)

        total_stored = 0
        watermark: float | None = None
        until_ts: float | None = None
        for page_num in range(_QUERY_POLL_MAX_PAGES):
            queries = await client.get_queries(
                from_ts=from_ts, until_ts=until_ts, length=_QUERY_POLL_PAGE_SIZE,
            )
            if page_num == 0:
                await save_sid(instance.id, client.sid)
                logger.info("Got %d queries from %s", len(queries), instance.name)
                _breaker_success(instance_key, instance.name)
            if not queries:
                break

            total_stored += await _store_queries(instance, queries)
            page_max = max(q.timestamp.timestamp() for q in queries)
            page_min = min(q.timestamp.timestamp() for q in queries)
            if watermark is None or page_max > watermark:
                watermark = page_max

            if len(queries) < _QUERY_POLL_PAGE_SIZE:
                break  # short page — the window is drained
            # Full page: rows older than page_min may remain. `until` is
            # inclusive, so the boundary row comes back on the next page and
            # is dropped by _store_queries' pihole_id dedup.
            if until_ts is not None and page_min >= until_ts:
                # No progress — a page-size burst shares one timestamp.
                break
            until_ts = page_min
        else:
            logger.warning(
                "Query poll for %s stopped at the %d-page cap with the window "
                "still full — rows between from=%s and until=%s were not "
                "fetched this tick (burst > %d rows). A backfill can recover "
                "the gap.",
                instance.name, _QUERY_POLL_MAX_PAGES, from_ts, until_ts,
                _QUERY_POLL_MAX_PAGES * _QUERY_POLL_PAGE_SIZE,
            )

        if total_stored:
            logger.info("Stored %d new queries for %s", total_stored, instance.name)
        # Advance the watermark to the most recent timestamp we've seen.
        if watermark is not None:
            _last_seen_ts[instance_key] = watermark

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
    now = datetime.now(UTC)
    recent_threshold = timedelta(minutes=10)

    # Check the most recent stored query for this instance.
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.max(QueryLog.timestamp)).where(QueryLog.instance_id == instance.id)
        )
        latest_ts: datetime | None = result.scalar_one_or_none()

    if latest_ts is not None and latest_ts.tzinfo is None:
        latest_ts = latest_ts.replace(tzinfo=UTC)

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
    # Slice the gap into one-hour windows so no single request can exceed the
    # per-request row cap even on a very busy hour.
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
