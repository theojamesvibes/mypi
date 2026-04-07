from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import get_db
from app.models.pihole import PiholeInstance, QueryLog
from app.models.user import User
from app.schemas.queries import QueryLogEntry, QueryLogPage

router = APIRouter(prefix="/api/queries", tags=["queries"])


@router.get("", response_model=QueryLogPage)
async def get_queries(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    instance_id: uuid.UUID | None = Query(default=None),
    domain: str | None = Query(default=None),
    client: str | None = Query(default=None),
    status: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=720),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    base_q = (
        select(QueryLog, PiholeInstance.name.label("instance_name"))
        .join(PiholeInstance, QueryLog.instance_id == PiholeInstance.id)
        .where(QueryLog.timestamp >= since)
    )

    if instance_id:
        base_q = base_q.where(QueryLog.instance_id == instance_id)
    if domain:
        base_q = base_q.where(QueryLog.domain.ilike(f"%{domain}%"))
    if client:
        base_q = base_q.where(
            (QueryLog.client_ip.ilike(f"%{client}%")) | (QueryLog.client_name.ilike(f"%{client}%"))
        )
    if status:
        base_q = base_q.where(QueryLog.status == status)

    count_q = select(func.count()).select_from(base_q.subquery())
    total_result = await db.execute(count_q)
    total = total_result.scalar_one()

    data_q = (
        base_q
        .order_by(QueryLog.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(data_q)
    rows = result.fetchall()

    items = []
    for row in rows:
        log, inst_name = row
        items.append(
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
        )

    return QueryLogPage(total=total, page=page, page_size=page_size, items=items)
