from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.pihole import PiholeInstance


class Site(Base):
    """A logical grouping of Pi-holes shown as one dashboard tab.

    Exactly one active site is the "Main" (`is_main`); non-Main sites inherit
    unset settings from it (see SiteSetting below). `slug` is the URL-safe name
    used in dashboard links (/dashboard/<slug>).
    """
    __tablename__ = "sites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    is_main: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    instances: Mapped[list[PiholeInstance]] = relationship(
        "PiholeInstance", back_populates="site", cascade="all, delete-orphan"
    )
    settings: Mapped[list[SiteSetting]] = relationship(
        "SiteSetting", back_populates="site", cascade="all, delete-orphan"
    )


class SiteSlugHistory(Base):
    """Remembers a site's previous URL name (slug) after a rename.

    Lets old bookmarks and links to the former /dashboard/<old-slug> keep
    working via a 301 redirect instead of 404-ing.
    """
    __tablename__ = "site_slug_history"

    old_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SiteSetting(Base):
    """Per-site key/value settings (e.g. poll interval, Pushover config).

    One row per (site, key). A NULL `value` — or no row at all — means
    "inherit from the active Main site" on read (see services/site_settings.py).
    """
    __tablename__ = "site_settings"

    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)

    site: Mapped[Site] = relationship("Site", back_populates="settings")
