from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, distinct, func, select
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

# Pi-hole v6 returns status as uppercase strings (e.g. "GRAVITY", "FORWARDED").
BLOCKED_STATUSES = frozenset({
    "GRAVITY", "REGEX", "BLACKLIST",
    "EXTERNAL_BLOCKED_IP", "EXTERNAL_BLOCKED_NULL", "EXTERNAL_BLOCKED_NXDOMAIN",
    "GRAVITY_CNAME", "REGEX_CNAME", "BLACKLIST_CNAME",
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
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is not None:
        since_dt = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    since = since_dt

    result = await db.execute(
        select(PiholeInstance)
        .where(PiholeInstance.is_active.is_(True))
        .order_by(PiholeInstance.is_master.desc(), PiholeInstance.name)
    )
    instances = result.scalars().all()
    snapshots = await _latest_snapshots_by_instance(db)

    # Compute aggregate query counts from QueryLog for the selected time range.
    agg_result = await db.execute(
        select(
            func.count(QueryLog.id).label("total"),
            func.count(case((QueryLog.status.in_(list(BLOCKED_STATUSES)), QueryLog.id))).label("blocked"),
            func.count(case((QueryLog.status.in_(["FORWARDED"]), QueryLog.id))).label("forwarded"),
            func.count(case((QueryLog.status.in_(["CACHE", "CACHE_STALE"]), QueryLog.id))).label("cached"),
            func.count(distinct(QueryLog.client_ip)).label("unique_clients"),
        )
        .where(QueryLog.timestamp >= since)
    )
    agg = agg_result.one()

    # Per-instance time-windowed aggregation from QueryLog.
    inst_agg_result = await db.execute(
        select(
            QueryLog.instance_id,
            func.count(QueryLog.id).label("total"),
            func.count(case((QueryLog.status.in_(list(BLOCKED_STATUSES)), QueryLog.id))).label("blocked"),
            func.count(distinct(QueryLog.client_ip)).label("unique_clients"),
        )
        .where(QueryLog.timestamp >= since)
        .group_by(QueryLog.instance_id)
    )
    inst_agg: dict[uuid.UUID, tuple[int, int, int]] = {
        row.instance_id: (row.total, row.blocked, row.unique_clients)
        for row in inst_agg_result.fetchall()
    }

    # Blocklist size is time-independent — take from the most recent snapshot.
    domains_on_blocklist = 0
    for snap in snapshots.values():
        if snap and snap.domains_on_blocklist:
            domains_on_blocklist = snap.domains_on_blocklist
            break

    total = agg.total or 0
    blocked = agg.blocked or 0
    percent_blocked = round(blocked / total * 100, 1) if total > 0 else 0.0

    totals = SummaryStats(
        dns_queries_today=total,
        queries_blocked=blocked,
        percent_blocked=percent_blocked,
        domains_on_blocklist=domains_on_blocklist,
        unique_clients=agg.unique_clients or 0,
        queries_cached=agg.cached or 0,
        queries_forwarded=agg.forwarded or 0,
    )

    per_instance = []
    for inst in instances:
        snap = snapshots.get(inst.id)
        i_total, i_blocked, i_clients = inst_agg.get(inst.id, (0, 0, 0))
        i_pct = round(i_blocked / i_total * 100, 1) if i_total > 0 else 0.0
        per_instance.append({
            "id": str(inst.id),
            "name": inst.name,
            "url": inst.url,
            "color": inst.color,
            "is_master": inst.is_master,
            "is_active": inst.is_active,
            "last_seen_at": inst.last_seen_at.isoformat() if inst.last_seen_at else None,
            "status": snap.status if snap else "unknown",
            # Time-windowed query counts from QueryLog:
            "dns_queries_today": i_total,
            "queries_blocked": i_blocked,
            "percent_blocked": i_pct,
            "unique_clients": i_clients,
            # Time-independent — from latest snapshot:
            "domains_on_blocklist": snap.domains_on_blocklist if snap else 0,
        })

    return AggregatedSummary(totals=totals, instances=per_instance)


@router.get("/history", response_model=HistoryResponse)
async def get_history(
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    bucket_minutes: int = Query(default=10, ge=1, le=1440),
    instance_id: uuid.UUID | None = Query(default=None),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is not None:
        since_dt = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    since = since_dt
    now = datetime.now(timezone.utc)

    # A bucket that had zero queries during an outage still needs to appear in
    # the response — otherwise the chart silently closes the gap and the outage
    # is invisible. Build the full contiguous bucket series via generate_series
    # and LEFT JOIN the aggregated counts, coalescing missing rows to zero.
    # date_bin (PG 14+) honours the full bucket_minutes stride.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    bucket_interval = func.make_interval(0, 0, 0, 0, 0, bucket_minutes)

    series_start = func.date_bin(bucket_interval, since, epoch)
    series_end = func.date_bin(bucket_interval, now, epoch)
    series = (
        select(
            func.generate_series(series_start, series_end, bucket_interval).label("bucket")
        )
        .subquery("series")
    )

    agg_bucket = func.date_bin(bucket_interval, QueryLog.timestamp, epoch).label("bucket")
    agg_q = (
        select(
            agg_bucket,
            func.count(QueryLog.id).label("queries"),
            func.count(
                case((QueryLog.status.in_(list(BLOCKED_STATUSES)), QueryLog.id))
            ).label("blocked"),
        )
        .where(QueryLog.timestamp >= since)
        .group_by(agg_bucket)
    )
    if instance_id:
        agg_q = agg_q.where(QueryLog.instance_id == instance_id)
    agg = agg_q.subquery("agg")

    q = (
        select(
            series.c.bucket,
            func.coalesce(agg.c.queries, 0).label("queries"),
            func.coalesce(agg.c.blocked, 0).label("blocked"),
        )
        .select_from(series.outerjoin(agg, series.c.bucket == agg.c.bucket))
        .order_by(series.c.bucket)
    )

    result = await db.execute(q)
    buckets = [
        HistoryBucket(timestamp=row.bucket, queries=row.queries, blocked=row.blocked)
        for row in result.fetchall()
    ]

    return HistoryResponse(buckets=buckets, instance_id=instance_id)


@router.get("/top", response_model=TopStatsResponse)
async def get_top(
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    instance_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is not None:
        since_dt = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        since = since_dt
    else:
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
