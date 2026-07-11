"""Housekeeping jobs and shutdown: retention cleanup, state pruning."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance, QueryLog, StatsSnapshot
from app.models.user import RevokedToken
from app.services.client_manager import close_all_clients, close_client
from app.services.collector.state import (
    _consec_failures,
    _cooldown_until,
    _last_failure_at,
    _last_seen_ts,
    _offline_alert_count,
    _offline_retry_count,
    _prev_dns_queries_today,
    _prev_status,
    _prev_watermark_for_stall,
    _site_poll_seq,
    _stall_alerted,
    _stall_count,
    _vip_active_node,
    _vip_advance_streak,
    _vip_group_stall_alerted,
    _vip_last_advance_seq,
)

logger = logging.getLogger(__name__)


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


async def cleanup_old_data() -> None:
    cutoff = datetime.now(UTC) - timedelta(days=settings.data_retention_days)
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        # DELETE statements always yield a CursorResult at runtime; the cast
        # exposes .rowcount, which the generic Result stub doesn't declare.
        snap_result = cast(CursorResult[Any], await db.execute(
            delete(StatsSnapshot).where(StatsSnapshot.collected_at < cutoff)
        ))
        query_result = cast(CursorResult[Any], await db.execute(
            delete(QueryLog).where(QueryLog.timestamp < cutoff)
        ))
        token_result = cast(CursorResult[Any], await db.execute(
            delete(RevokedToken).where(RevokedToken.expires_at < now)
        ))
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
