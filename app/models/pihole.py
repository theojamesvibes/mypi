from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PiholeInstance(Base):
    __tablename__ = "pihole_instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_password: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#3c8dbc")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    snapshots: Mapped[list[StatsSnapshot]] = relationship(
        "StatsSnapshot", back_populates="instance", cascade="all, delete-orphan"
    )
    query_logs: Mapped[list[QueryLog]] = relationship(
        "QueryLog", back_populates="instance", cascade="all, delete-orphan"
    )


class StatsSnapshot(Base):
    __tablename__ = "stats_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pihole_instances.id", ondelete="CASCADE"), nullable=False
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="online")

    dns_queries_today: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    queries_blocked: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    percent_blocked: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    domains_on_blocklist: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unique_clients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queries_cached: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    queries_forwarded: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    instance: Mapped[PiholeInstance] = relationship("PiholeInstance", back_populates="snapshots")

    __table_args__ = (
        Index("ix_stats_instance_collected", "instance_id", "collected_at"),
    )


class QueryLog(Base):
    __tablename__ = "query_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pihole_instances.id", ondelete="CASCADE"), nullable=False
    )
    pihole_query_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    query_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reply_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reply_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    instance: Mapped[PiholeInstance] = relationship("PiholeInstance", back_populates="query_logs")

    __table_args__ = (
        Index("ix_querylog_instance_ts", "instance_id", "timestamp"),
        Index("ix_querylog_domain", "domain"),
        Index("ix_querylog_client", "client_ip"),
        Index("ix_querylog_pihole_id", "instance_id", "pihole_query_id"),
    )
