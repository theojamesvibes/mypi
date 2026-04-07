from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.pihole import PiholeInstance, QueryLog, StatsSnapshot
from app.models.user import User
from app.schemas.stats import (
    AggregatedSummary,
    HistoryBucket,
    HistoryResponse,
    SummaryStats,
    TopClient,
    TopDomain,
    TopStatsResponse,
)

router = APIRouter(prefix="/api/stats", tags=["stats"])

BLOCKED_STATUSES = frozenset({
    "blocked_gravity", "blocked_regex", "blocked_denylist", "blocked_nxdomain",
    "blocked_cname_gravity", "blocked_cname_regex", "blocked_cname_denylist",
    "blocked_gravity_cname",
})


async def _latest_snapshots_by_instance(db: AsyncSession) -> dict[uuid.UUID, StatsSnapshot]:
    """Return the most recent StatsSnapshot per instance in a single query."""
    max_subq = (
        select(
            StatsSnapshot.instance_id,
            func.max(StatsSnapshot.collected_at).label("max_ts"),
        )
        .group_by(StatsSnapshot.instance_id)
        .subquery()
    )
    result = await db.execute(
        select(StatsSnapshot).join(
            max_subq,
            and_(
                StatsSnapshot.instance_id == max_subq.c.instance_id,
                StatsSnapshot.collected_at == max_subq.c.max_ts,
            ),
        )
    )
    return {snap.instance_id: snap for snap in result.scalars().all()}


@router.get("/summary", response_model=AggregatedSummary)
async def get_summary(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PiholeInstance).where(PiholeInstance.is_active.is_(True)))
    instances = result.scalars().all()
    snapshots = await _latest_snapshots_by_instance(db)

    per_instance = []
    totals = SummaryStats(
        dns_queries_today=0,
        queries_blocked=0,
        percent_blocked=0.0,
        domains_on_blocklist=0,
        unique_clients=0,
        queries_cached=0,
        queries_forwarded=0,
    )

    for inst in instances:
        snap = snapshots.get(inst.id)
        per_instance.append({
            "id": str(inst.id),
            "name": inst.name,
            "color": inst.color,
            "status": snap.status if snap else "unknown",
            "dns_queries_today": snap.dns_queries_today if snap else 0,
            "queries_blocked": snap.queries_blocked if snap else 0,
            "percent_blocked": snap.percent_blocked if snap else 0.0,
            "domains_on_blocklist": snap.domains_on_blocklist if snap else 0,
            "unique_clients": snap.unique_clients if snap else 0,
            "queries_cached": snap.queries_cached if snap else 0,
            "queries_forwarded": snap.queries_forwarded if snap else 0,
        })

        if snap and snap.status == "online":
            totals.dns_queries_today += snap.dns_queries_today
            totals.queries_blocked += snap.queries_blocked
            totals.domains_on_blocklist += snap.domains_on_blocklist
            totals.unique_clients += snap.unique_clients
            totals.queries_cached += snap.queries_cached
            totals.queries_forwarded += snap.queries_forwarded

    if totals.dns_queries_today > 0:
        totals.percent_blocked = round(totals.queries_blocked / totals.dns_queries_today * 100, 1)

    return AggregatedSummary(totals=totals, instances=per_instance)


@router.get("/history", response_model=HistoryResponse)
async def get_history(
    hours: int = Query(default=24, ge=1, le=168),
    instance_id: uuid.UUID | None = Query(default=None),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    q = select(StatsSnapshot).where(StatsSnapshot.collected_at >= since)
    if instance_id:
        q = q.where(StatsSnapshot.instance_id == instance_id)
    q = q.order_by(StatsSnapshot.collected_at)

    result = await db.execute(q)
    snapshots = result.scalars().all()

    buckets: dict[int, HistoryBucket] = {}
    for snap in snapshots:
        bucket_ts = snap.collected_at.replace(second=0, microsecond=0)
        bucket_ts = bucket_ts.replace(minute=bucket_ts.minute - (bucket_ts.minute % 10))
        key = int(bucket_ts.timestamp())
        if key not in buckets:
            buckets[key] = HistoryBucket(timestamp=bucket_ts, queries=0, blocked=0)
        buckets[key].queries += snap.dns_queries_today
        buckets[key].blocked += snap.queries_blocked

    return HistoryResponse(
        buckets=sorted(buckets.values(), key=lambda b: b.timestamp),
        instance_id=instance_id,
    )


@router.get("/top", response_model=TopStatsResponse)
async def get_top(
    hours: int = Query(default=24, ge=1, le=168),
    instance_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    domain_q = (
        select(QueryLog.domain, QueryLog.status, func.count(QueryLog.id).label("cnt"))
        .where(QueryLog.timestamp >= since)
        .group_by(QueryLog.domain, QueryLog.status)
    )
    if instance_id:
        domain_q = domain_q.where(QueryLog.instance_id == instance_id)

    client_q = (
        select(QueryLog.client_ip, QueryLog.client_name, func.count(QueryLog.id).label("cnt"))
        .where(QueryLog.timestamp >= since)
        .group_by(QueryLog.client_ip, QueryLog.client_name)
        .order_by(func.count(QueryLog.id).desc())
        .limit(limit)
    )
    if instance_id:
        client_q = client_q.where(QueryLog.instance_id == instance_id)

    domain_result, client_result = (
        await db.execute(domain_q),
        await db.execute(client_q),
    )

    permitted: dict[str, int] = {}
    blocked: dict[str, int] = {}
    for domain, status, cnt in domain_result.fetchall():
        if not domain:
            continue
        if status in BLOCKED_STATUSES:
            blocked[domain] = blocked.get(domain, 0) + cnt
        else:
            permitted[domain] = permitted.get(domain, 0) + cnt

    return TopStatsResponse(
        top_permitted=[
            TopDomain(domain=d, count=c)
            for d, c in sorted(permitted.items(), key=lambda x: x[1], reverse=True)[:limit]
        ],
        top_blocked=[
            TopDomain(domain=d, count=c)
            for d, c in sorted(blocked.items(), key=lambda x: x[1], reverse=True)[:limit]
        ],
        top_clients=[
            TopClient(client=row.client_name or row.client_ip or "", count=row.cnt)
            for row in client_result.fetchall()
        ],
        instance_id=instance_id,
    )
