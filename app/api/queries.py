from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._site_dep import resolve_site
from app.api.stats import BLOCKED_STATUSES, _site_instance_ids
from app.auth import get_current_user
from app.database import get_db
from app.models.pihole import PiholeInstance, QueryLog
from app.models.site import Site
from app.models.user import User
from app.schemas.queries import ClientSummary, QueryLogEntry, QueryLogPage

router = APIRouter(prefix="/api/queries", tags=["queries"])


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards so user input is treated as a literal substring."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_SORT_COLUMNS = {
    "timestamp": QueryLog.timestamp,
    "domain": QueryLog.domain,
    "client": QueryLog.client_name,
    "status": QueryLog.status,
    "reply_time_ms": QueryLog.reply_time_ms,
    "query_type": QueryLog.query_type,
}


@router.get("", response_model=QueryLogPage)
async def get_queries(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    instance_id: uuid.UUID | None = Query(default=None),
    domain: str | None = Query(default=None),
    client: str | None = Query(default=None),
    status: str | None = Query(default=None),
    blocked: bool | None = Query(default=None, description="true=blocked only, false=permitted only"),
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None, description="ISO datetime; overrides hours when provided"),
    sort_by: str = Query(default="timestamp"),
    sort_dir: str = Query(default="desc"),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

    base_q = (
        select(QueryLog, PiholeInstance.name.label("instance_name"))
        .join(PiholeInstance, QueryLog.instance_id == PiholeInstance.id)
        .where(QueryLog.timestamp >= since)
    )

    if instance_id:
        base_q = base_q.where(QueryLog.instance_id == instance_id)
    if domain:
        base_q = base_q.where(QueryLog.domain.ilike(f"%{_escape_like(domain)}%", escape="\\"))
    if client:
        escaped_client = _escape_like(client)
        base_q = base_q.where(
            (QueryLog.client_ip.ilike(f"%{escaped_client}%", escape="\\"))
            | (QueryLog.client_name.ilike(f"%{escaped_client}%", escape="\\"))
        )
    if status:
        base_q = base_q.where(QueryLog.status == status)
    if blocked is True:
        base_q = base_q.where(QueryLog.status.in_(list(BLOCKED_STATUSES)))
    elif blocked is False:
        base_q = base_q.where(QueryLog.status.not_in(list(BLOCKED_STATUSES)))

    count_q = select(func.count()).select_from(base_q.subquery())
    total_result = await db.execute(count_q)
    total = total_result.scalar_one()

    sort_col = _SORT_COLUMNS.get(sort_by, QueryLog.timestamp)
    order_expr = sort_col.asc() if sort_dir == "asc" else sort_col.desc()

    data_q = (
        base_q
        .order_by(order_expr)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(data_q)
    rows = result.fetchall()

    items = [
        QueryLogEntry(
            id=log.id,
            instance_id=log.instance_id,
            instance_name=inst_name or "",
            timestamp=log.timestamp,
            client_ip=log.client_ip,
            client_name=log.client_name,
            query_type=log.query_type,
            domain=log.domain,
            status=log.status,
            reply_type=log.reply_type,
            reply_time_ms=log.reply_time_ms,
        )
        for log, inst_name in rows
    ]

    return QueryLogPage(total=total, page=page, page_size=page_size, items=items)


@router.get("/clients", response_model=list[ClientSummary])
async def get_client_summary(
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None, description="ISO datetime; overrides hours when provided"),
    instance_id: uuid.UUID | None = Query(default=None),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one row per unique client with aggregate query counts."""
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

    q = (
        select(
            QueryLog.client_ip,
            QueryLog.client_name,
            func.count(QueryLog.id).label("total_queries"),
            func.count(
                case((QueryLog.status.in_(list(BLOCKED_STATUSES)), QueryLog.id))
            ).label("blocked_queries"),
            func.max(QueryLog.timestamp).label("last_seen"),
        )
        .where(QueryLog.timestamp >= since)
        .group_by(QueryLog.client_ip, QueryLog.client_name)
        .order_by(func.count(QueryLog.id).desc())
    )
    if instance_id:
        q = q.where(QueryLog.instance_id == instance_id)

    result = await db.execute(q)
    return [
        ClientSummary(
            client_ip=row.client_ip or "",
            client_name=row.client_name or "",
            total_queries=row.total_queries,
            blocked_queries=row.blocked_queries,
            last_seen=row.last_seen,
        )
        for row in result.fetchall()
    ]


# ── Per-site variants ────────────────────────────────────────────────────────

site_router = APIRouter(prefix="/api/sites/{slug}/queries", tags=["queries (per-site)"])


@site_router.get("", response_model=QueryLogPage)
async def get_queries_for_site(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    instance_id: uuid.UUID | None = Query(default=None),
    domain: str | None = Query(default=None),
    client: str | None = Query(default=None),
    status: str | None = Query(default=None),
    blocked: bool | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    sort_by: str = Query(default="timestamp"),
    sort_dir: str = Query(default="desc"),
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

    site_ids = await _site_instance_ids(db, site.id)
    if not site_ids:
        return QueryLogPage(total=0, page=page, page_size=page_size, items=[])

    base_q = (
        select(QueryLog, PiholeInstance.name.label("instance_name"))
        .join(PiholeInstance, QueryLog.instance_id == PiholeInstance.id)
        .where(QueryLog.timestamp >= since)
        .where(QueryLog.instance_id.in_(site_ids))
    )

    if instance_id:
        base_q = base_q.where(QueryLog.instance_id == instance_id)
    if domain:
        base_q = base_q.where(QueryLog.domain.ilike(f"%{_escape_like(domain)}%", escape="\\"))
    if client:
        escaped_client = _escape_like(client)
        base_q = base_q.where(
            (QueryLog.client_ip.ilike(f"%{escaped_client}%", escape="\\"))
            | (QueryLog.client_name.ilike(f"%{escaped_client}%", escape="\\"))
        )
    if status:
        base_q = base_q.where(QueryLog.status == status)
    if blocked is True:
        base_q = base_q.where(QueryLog.status.in_(list(BLOCKED_STATUSES)))
    elif blocked is False:
        base_q = base_q.where(QueryLog.status.not_in(list(BLOCKED_STATUSES)))

    count_q = select(func.count()).select_from(base_q.subquery())
    total_result = await db.execute(count_q)
    total = total_result.scalar_one()

    sort_col = _SORT_COLUMNS.get(sort_by, QueryLog.timestamp)
    order_expr = sort_col.asc() if sort_dir == "asc" else sort_col.desc()

    data_q = base_q.order_by(order_expr).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(data_q)
    rows = result.fetchall()

    items = [
        QueryLogEntry(
            id=log.id,
            instance_id=log.instance_id,
            instance_name=inst_name or "",
            timestamp=log.timestamp,
            client_ip=log.client_ip,
            client_name=log.client_name,
            query_type=log.query_type,
            domain=log.domain,
            status=log.status,
            reply_type=log.reply_type,
            reply_time_ms=log.reply_time_ms,
        )
        for log, inst_name in rows
    ]

    return QueryLogPage(total=total, page=page, page_size=page_size, items=items)


@site_router.get("/clients", response_model=list[ClientSummary])
async def get_client_summary_for_site(
    hours: int = Query(default=24, ge=1, le=720),
    since: datetime | None = Query(default=None),
    instance_id: uuid.UUID | None = Query(default=None),
    site: Site = Depends(resolve_site),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

    site_ids = await _site_instance_ids(db, site.id)
    if not site_ids:
        return []

    q = (
        select(
            QueryLog.client_ip,
            QueryLog.client_name,
            func.count(QueryLog.id).label("total_queries"),
            func.count(
                case((QueryLog.status.in_(list(BLOCKED_STATUSES)), QueryLog.id))
            ).label("blocked_queries"),
            func.max(QueryLog.timestamp).label("last_seen"),
        )
        .where(QueryLog.timestamp >= since)
        .where(QueryLog.instance_id.in_(site_ids))
        .group_by(QueryLog.client_ip, QueryLog.client_name)
        .order_by(func.count(QueryLog.id).desc())
    )
    if instance_id:
        q = q.where(QueryLog.instance_id == instance_id)

    result = await db.execute(q)
    return [
        ClientSummary(
            client_ip=row.client_ip or "",
            client_name=row.client_name or "",
            total_queries=row.total_queries,
            blocked_queries=row.blocked_queries,
            last_seen=row.last_seen,
        )
        for row in result.fetchall()
    ]
