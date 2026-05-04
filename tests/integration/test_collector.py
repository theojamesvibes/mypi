"""Integration tests for collector.py functions that touch the DB."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


# ── Reset collector state between tests ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_collector_state():
    from app.services import collector
    state_dicts = (
        collector._consec_failures, collector._cooldown_until,
        collector._last_failure_at, collector._last_seen_ts,
        collector._prev_status, collector._offline_retry_count,
        collector._offline_alert_count, collector._stall_count,
        collector._stall_alerted, collector._prev_dns_queries_today,
        collector._prev_watermark_for_stall,
        collector._vip_last_advance_seq, collector._vip_advance_streak,
        collector._vip_active_node, collector._vip_group_stall_alerted,
        collector._site_poll_seq,
    )
    for d in state_dicts:
        d.clear()
    yield
    for d in state_dicts:
        d.clear()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def site(db_session):
    from app.models.site import Site
    s = Site(name="Test", slug="test", is_active=True, is_main=True)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def instance(db_session, site):
    from cryptography.fernet import Fernet
    from app.config import settings
    from app.models.pihole import PiholeInstance
    import app.models.pihole as pihole_models

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None

    inst = PiholeInstance(
        site_id=site.id, name="pihole-1",
        url="http://pihole.test", api_password="pw",
        is_active=True,
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)
    return inst


# ── prune_inactive_state ─────────────────────────────────────────────────────


async def test_prune_inactive_state_drops_state_for_deactivated_instances(
    instance, db_session,
):
    """When config_loader deactivates an instance (is_active=False),
    the per-instance dicts must shed its key so the next reload picks
    up clean state."""
    from app.services import collector

    key = str(instance.id)
    collector._last_seen_ts[key] = 12345.0
    collector._prev_status[key] = "online"
    collector._consec_failures[key] = 2

    instance.is_active = False
    db_session.add(instance)
    await db_session.commit()

    await collector.prune_inactive_state()

    assert key not in collector._last_seen_ts
    assert key not in collector._prev_status
    assert key not in collector._consec_failures


async def test_prune_inactive_state_keeps_state_for_active_instances(
    instance,
):
    from app.services import collector

    key = str(instance.id)
    collector._last_seen_ts[key] = 12345.0

    await collector.prune_inactive_state()

    # Active instance — state must survive.
    assert collector._last_seen_ts[key] == 12345.0


# ── cleanup_old_data ─────────────────────────────────────────────────────────


async def test_cleanup_old_data_removes_rows_past_retention(
    instance, db_session,
):
    """Stats snapshots, query logs, and revoked-token rows older than
    DATA_RETENTION_DAYS get deleted nightly."""
    from app.config import settings
    from app.models.pihole import StatsSnapshot, QueryLog
    from app.models.user import RevokedToken
    from app.services import collector

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_retention_days + 1)

    db_session.add_all([
        StatsSnapshot(
            instance_id=instance.id, collected_at=cutoff, status="online",
        ),
        QueryLog(
            instance_id=instance.id, timestamp=cutoff,
            domain="ancient.example", status="OK",
        ),
        RevokedToken(jti="ancient-jti", expires_at=cutoff),
    ])
    # Recent rows must SURVIVE.
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add_all([
        StatsSnapshot(
            instance_id=instance.id, collected_at=recent, status="online",
        ),
        QueryLog(
            instance_id=instance.id, timestamp=recent,
            domain="recent.example", status="OK",
        ),
    ])
    await db_session.commit()

    await collector.cleanup_old_data()

    # Use a fresh session — the autouse db_session has the deleted rows
    # in its identity map.
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh:
        snapshots = (await fresh.execute(select(StatsSnapshot))).scalars().all()
        queries = (await fresh.execute(select(QueryLog))).scalars().all()
        tokens = (await fresh.execute(select(RevokedToken))).scalars().all()

    # Only the recent rows should remain.
    assert len(snapshots) == 1
    assert len(queries) == 1
    assert queries[0].domain == "recent.example"
    assert len(tokens) == 0
