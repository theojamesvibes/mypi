from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    """A dashboard login account (username + hashed password).

    Also tracks failed-login lockout state and owns any API keys the user has
    issued for iOS / automation access.
    """
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_change_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    # Consecutive failed login counter. Reset to 0 on success. When it
    # crosses settings.login_lockout_threshold the auth handler stamps
    # `failed_login_lockout_until` and refuses subsequent logins (with
    # the same 401 shape as a wrong password — no enumeration leak).
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_login_lockout_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    api_keys: Mapped[list[ApiKey]] = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base):
    """An X-API-Key credential for iOS / automation clients.

    Only the hash of the key is stored (`key_hash`); the raw key is shown to
    the user once at creation and never again. `is_read_only` blocks mutating
    actions when set.
    """
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    key_hash_algo: Mapped[str] = mapped_column(String(16), nullable=False, server_default="sha256")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_read_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    # NULL = key has access to every site on this deployment (v1 default).
    # Populated list = key is scoped to those site ids only (reserved for
    # a future release; not user-configurable yet).
    allowed_site_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="api_keys")


class RevokedToken(Base):
    """JTI denylist for logged-out JWT sessions.

    Rows are cleaned up nightly alongside old query data once the token's
    natural expiry has passed, so the table stays small.
    """
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
