from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base

if TYPE_CHECKING:
    from app.models.site import Site

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        from app.config import settings
        _fernet = Fernet(settings.encryption_key.encode())
    return _fernet


class EncryptedString(TypeDecorator):
    """Transparent Fernet encryption/decryption for string columns.

    Stores ciphertext as a VARCHAR; encrypts on write, decrypts on read.
    If decryption fails (e.g. a legacy plaintext row before encryption was
    added), returns an empty string — config_loader will re-sync the correct
    value from pihole_instances.yml on the next startup.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if not value:
            return value
        return _get_fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if not value:
            return value
        try:
            return _get_fernet().decrypt(value.encode()).decode()
        except (InvalidToken, Exception):
            logger.warning(
                "api_password column contains a value that could not be decrypted "
                "(likely a pre-encryption plaintext row). Returning empty string — "
                "value will be re-synced from pihole_instances.yml on next startup."
            )
            return ""


class PiholeInstance(Base):
    """One monitored Pi-hole server (one row = one Pi-hole box).

    Rows are created and kept in sync from `pihole_instances.yml` at startup;
    the `version_*` / `update_available_*` / `last_seen_at` columns are filled
    in later by the background stats poller. Note two distinct notions of
    "primary": `is_master` marks the Pi-hole that owns the config we sync out
    to the others (teleporter master), while `vip_role` records keepalived VIP
    cluster membership — they are independent.
    """
    __tablename__ = "pihole_instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_password: Mapped[str] = mapped_column(EncryptedString(512), nullable=False, default="")
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#3c8dbc")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    is_master: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # NULL / "master" / "replica" — VIP cluster membership. See migration 0016.
    vip_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Opaque Pi-hole login token (X-FTL-SID). Persisted so we can reuse the
    # existing session across container restarts instead of re-authenticating.
    session_sid: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Software versions (populated by the stats poller, persisted across restarts)
    version_core: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_ftl: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_web: Mapped[str | None] = mapped_column(String(64), nullable=True)
    update_available_core: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    update_available_ftl: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    update_available_web: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    site: Mapped[Site] = relationship("Site", back_populates="instances")
    snapshots: Mapped[list[StatsSnapshot]] = relationship(
        "StatsSnapshot", back_populates="instance", cascade="all, delete-orphan"
    )
    query_logs: Mapped[list[QueryLog]] = relationship(
        "QueryLog", back_populates="instance", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("site_id", "name", name="uq_pihole_instances_site_name"),
    )


class StatsSnapshot(Base):
    """A point-in-time copy of one Pi-hole's headline counters.

    Taken every 60s by the background poller; powers the dashboard's history
    graphs. Old rows are pruned by the nightly cleanup job after
    DATA_RETENTION_DAYS.
    """
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
    """One DNS lookup as seen by a Pi-hole — who asked, what domain, and
    whether it was blocked, cached, or forwarded.

    This is the high-volume table (one row per DNS query pulled from Pi-hole);
    the four indexes below keep the dashboard's client/domain/time filters
    fast. Pruned by the nightly cleanup job after DATA_RETENTION_DAYS.
    """
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
    # Pi-hole's id for the list that blocked this query (adlist for gravity,
    # else domainlist); NULL when the query wasn't blocked by a list. Resolved
    # to a list name via PiholeList for the "Blocked by list" breakdown.
    list_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    instance: Mapped[PiholeInstance] = relationship("PiholeInstance", back_populates="query_logs")

    __table_args__ = (
        Index("ix_querylog_instance_ts", "instance_id", "timestamp"),
        Index("ix_querylog_domain", "domain"),
        Index("ix_querylog_client", "client_ip"),
        Index("ix_querylog_pihole_id", "instance_id", "pihole_query_id"),
    )


class PiholeList(Base):
    """A Pi-hole adlist/allowlist, mirrored so the dashboard can resolve a
    blocked query's `list_id` to a human name and know whether the list is a
    security/threat feed.

    Synced periodically from each instance's GET /api/lists. `is_security` is
    computed at sync time from group membership (the configured
    `SECURITY_GROUP_NAME`), so a block attributed to one of these lists can be
    shown as a threat block rather than an ad/tracker block.
    """
    __tablename__ = "pihole_lists"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pihole_instances.id", ondelete="CASCADE"), nullable=False
    )
    # Pi-hole's own id for the list (matches QueryLog.list_id for gravity blocks).
    pihole_list_id: Mapped[int] = mapped_column(Integer, nullable=False)
    list_type: Mapped[str] = mapped_column(String(16), nullable=False, default="block")
    address: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    comment: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_security: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # One row per (instance, Pi-hole list id, type): adlist and domainlist
        # id-spaces overlap, so type is part of the key.
        UniqueConstraint("instance_id", "pihole_list_id", "list_type", name="uq_piholelist_instance_listid_type"),
        Index("ix_piholelist_instance", "instance_id"),
    )
