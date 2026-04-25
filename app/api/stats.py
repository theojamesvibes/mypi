from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, distinct, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._site_dep import resolve_site
from app.auth import get_current_user
from app.database import get_db
from app.models.pihole import PiholeInstance, QueryLog, StatsSnapshot
from app.models.site import Site
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
from app.services.site_settings import get_json_setting, get_main_site_id

# Top Clients self-traffic filter setting key. JSON boolean. Default true.
# Mirrors Pi-hole's own admin UI behaviour, which hides queries originating
# from Pi-hole itself so the Top Clients table reflects real LAN clients
# instead of Pi-hole's hourly PTR refresh, gravity URL resolution, version
# checks, etc.
HIDE_SELF_IN_TOP_CLIENTS_KEY = "hide_pihole_self_in_top_clients"
HIDE_SELF_DEFAULT = True
# Names Pi-hole logs against its own self-traffic (resolved against
# Pi-hole's hosts file or via 127.0.0.1 reverse-resolution).
_SELF_NAME_ALIASES = frozenset({"pi.hole", "localhost"})
# Loopback IPs. Pi-hole's own daemons sometimes show up with these as
# client_ip (e.g. when client_name reverse-resolution fails).
_SELF_IP_ALIASES = frozenset({"127.0.0.1", "::1"})

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


async def _summary_body(
    db: AsyncSession,
    since: datetime,
    site_id: uuid.UUID | None = None,
) -> AggregatedSummary:
    """Shared body for summary endpoints. site_id=None → all active
    instances across every site (legacy behavior)."""
    inst_filters = [PiholeInstance.is_active.is_(True)]
    if site_id is not None:
        inst_filters.append(PiholeInstance.site_id == site_id)
    # Join Site so the per-instance payload can carry site name/slug for the
    # Combined view's instance table. Site rows are always present for active
    # instances (FK enforced); a LEFT join only for safety against orphan rows.
    result = await db.execute(
        select(PiholeInstance, Site)
        .outerjoin(Site, PiholeInstance.site_id == Site.id)
        .where(*inst_filters)
        .order_by(PiholeInstance.is_master.desc(), PiholeInstance.name)
    )
    instances_with_site = result.all()
    instances = [row[0] for row in instances_with_site]
    site_by_instance: dict[uuid.UUID, Site | None] = {
        row[0].id: row[1] for row in instances_with_site
    }
    all_snapshots = await _latest_snapshots_by_instance(db)

    # When scoping to a site, constrain QueryLog aggregates + snapshot lookups
    # to that site's instances.
    inst_ids = [i.id for i in instances]
    if site_id is not None:
        inst_id_set = set(inst_ids)
        snapshots = {k: v for k, v in all_snapshots.items() if k in inst_id_set}
    else:
        snapshots = all_snapshots
    query_filters = [QueryLog.timestamp >= since]
    if site_id is not None:
        if not inst_ids:
            # Site has zero active instances; return empty totals.
            totals = SummaryStats(
                dns_queries_today=0, queries_blocked=0, percent_blocked=0.0,
                domains_on_blocklist=0, unique_clients=0,
                queries_cached=0, queries_forwarded=0,
            )
            return AggregatedSummary(totals=totals, instances=[])
        query_filters.append(QueryLog.instance_id.in_(inst_ids))

    agg_result = await db.execute(
        select(
            func.count(QueryLog.id).label("total"),
            func.count(case((QueryLog.status.in_(list(BLOCKED_STATUSES)), QueryLog.id))).label("blocked"),
            func.count(case((QueryLog.status.in_(["FORWARDED"]), QueryLog.id))).label("forwarded"),
            func.count(case((QueryLog.status.in_(["CACHE", "CACHE_STALE"]), QueryLog.id))).label("cached"),
            func.count(distinct(QueryLog.client_ip)).label("unique_clients"),
        )
        .where(*query_filters)
    )
    agg = agg_result.one()

    inst_agg_result = await db.execute(
        select(
            QueryLog.instance_id,
            func.count(QueryLog.id).label("total"),
            func.count(case((QueryLog.status.in_(list(BLOCKED_STATUSES)), QueryLog.id))).label("blocked"),
            func.count(distinct(QueryLog.client_ip)).label("unique_clients"),
        )
        .where(*query_filters)
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
        site = site_by_instance.get(inst.id)
        per_instance.append({
            "id": str(inst.id),
            "name": inst.name,
            "url": inst.url,
            "color": inst.color,
            "is_master": inst.is_master,
            "vip_role": inst.vip_role,
            "is_active": inst.is_active,
            "last_seen_at": inst.last_seen_at.isoformat() if inst.last_seen_at else None,
            "status": snap.status if snap else "unknown",
            # Site attribution — consumed by the Combined view.
            "site_id": str(site.id) if site else None,
            "site_name": site.name if site else None,
            "site_slug": site.slug if site else None,
            # Time-windowed query counts from QueryLog:
            "dns_queries_today": i_total,
            "queries_blocked": i_blocked,
            "percent_blocked": i_pct,
            "unique_clients": i_clients,
            # Time-independent — from latest snapshot:
            "domains_on_blocklist": snap.domains_on_blocklist if snap else 0,
        })

    return AggregatedSummary(totals=totals, instances=per_instance)


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
    return await _summary_body(db, since_dt, site_id=None)


async def _site_instance_ids(db: AsyncSession, site_id: uuid.UUID) -> list[uuid.UUID]:
    result = await db.execute(
        select(PiholeInstance.id).where(
            PiholeInstance.site_id == site_id,
            PiholeInstance.is_active.is_(True),
        )
    )
    return [row[0] for row in result.fetchall()]


async def _should_hide_pihole_self(
    db: AsyncSession, site_id: uuid.UUID | None,
) -> bool:
    """Resolve the Top Clients self-filter toggle. Reads from the site's
    site_settings (with main fallback); for the global endpoint
    (`site_id is None`) we read from main directly. Default true."""
    target_id = site_id if site_id is not None else await get_main_site_id(db)
    if target_id is None:
        return HIDE_SELF_DEFAULT
    return bool(await get_json_setting(
        db, target_id, HIDE_SELF_IN_TOP_CLIENTS_KEY, default=HIDE_SELF_DEFAULT,
    ))


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
    now = datetime.now(timezone.utc)
    return await _history_body(db, since_dt, now, bucket_minutes, instance_id, site_instance_ids=None)


async def _history_body(
    db: AsyncSession,
    since: datetime,
    now: datetime,
    bucket_minutes: int,
    instance_id: uuid.UUID | None = None,
    site_instance_ids: list[uuid.UUID] | None = None,
) -> "HistoryResponse":
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
    if site_instance_ids is not None:
        if not site_instance_ids:
            return HistoryResponse(buckets=[], instance_id=instance_id)
        agg_q = agg_q.where(QueryLog.instance_id.in_(site_instance_ids))
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
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    hide_self = await _should_hide_pihole_self(db, site_id=None)
    return await _top_body(
        db, since_dt, instance_id, limit,
        site_instance_ids=None, hide_self=hide_self,
    )


async def _top_body(
    db: AsyncSession,
    since: datetime,
    instance_id: uuid.UUID | None,
    limit: int,
    site_instance_ids: list[uuid.UUID] | None = None,
    hide_self: bool = False,
) -> TopStatsResponse:
    domain_q = (
        select(QueryLog.domain, QueryLog.status, func.count(QueryLog.id).label("cnt"))
        .where(QueryLog.timestamp >= since)
        .group_by(QueryLog.domain, QueryLog.status)
    )
    if instance_id:
        domain_q = domain_q.where(QueryLog.instance_id == instance_id)
    if site_instance_ids is not None:
        if not site_instance_ids:
            return TopStatsResponse(
                top_permitted=[], top_blocked=[], top_clients=[],
                instance_id=instance_id,
            )
        domain_q = domain_q.where(QueryLog.instance_id.in_(site_instance_ids))

    client_q = (
        select(QueryLog.client_ip, QueryLog.client_name, func.count(QueryLog.id).label("cnt"))
        .where(QueryLog.timestamp >= since)
        .group_by(QueryLog.client_ip, QueryLog.client_name)
        .order_by(func.count(QueryLog.id).desc())
        .limit(limit)
    )
    if instance_id:
        client_q = client_q.where(QueryLog.instance_id == instance_id)
    if site_instance_ids is not None:
        client_q = client_q.where(QueryLog.instance_id.in_(site_instance_ids))
    if hide_self:
        # Filter Pi-hole's own self-traffic. Each branch is NULL-guarded
        # because SQL three-valued logic would otherwise drop every row
        # whose client_name is NULL (LAN clients without PTR records) —
        # `NULL IN (...)` evaluates to NULL, `FALSE OR NULL = NULL`, and
        # `NOT NULL` is NULL, which fails the WHERE.
        #
        # Filter is name-aliases + loopback-IPs only. Earlier dev.17 also
        # excluded the configured Pi-hole instances' LAN IPs, but Pi-hole
        # already logs self-traffic with client_name = "pi.hole" (or
        # 127.0.0.1), so the LAN-IP exclusion was redundant and risked
        # hiding the actual Pi-hole host if it happened to also be a DNS
        # client of itself.
        name_match = and_(
            QueryLog.client_name.is_not(None),
            QueryLog.client_name.in_(list(_SELF_NAME_ALIASES)),
        )
        ip_match = and_(
            QueryLog.client_ip.is_not(None),
            QueryLog.client_ip.in_(list(_SELF_IP_ALIASES)),
        )
        client_q = client_q.where(not_(or_(name_match, ip_match)))

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


# ── Per-site variants ────────────────────────────────────────────────────────

site_router = APIRouter(prefix="/api/sites/{slug}/stats", tags=["stats (per-site)"])


@site_router.get("/summary", response_model=AggregatedSummary)
async def get_summary_for_site(
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is not None:
        since_dt = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return await _summary_body(db, since_dt, site_id=site.id)


@site_router.get("/history", response_model=HistoryResponse)
async def get_history_for_site(
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    bucket_minutes: int = Query(default=10, ge=1, le=1440),
    instance_id: uuid.UUID | None = Query(default=None),
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is not None:
        since_dt = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    now = datetime.now(timezone.utc)
    scope = await _site_instance_ids(db, site.id)
    return await _history_body(db, since_dt, now, bucket_minutes, instance_id, site_instance_ids=scope)


@site_router.get("/top", response_model=TopStatsResponse)
async def get_top_for_site(
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    instance_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is not None:
        since_dt = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    scope = await _site_instance_ids(db, site.id)
    hide_self = await _should_hide_pihole_self(db, site_id=site.id)
    return await _top_body(
        db, since_dt, instance_id, limit,
        site_instance_ids=scope, hide_self=hide_self,
    )
