from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AppSetting(Base):
    """Generic application-wide key/value settings store.

    One row per setting (session timeout, version-check state, Pushover
    config, …); `value` holds JSON text. Unlike SiteSetting these are global,
    not per-site.
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
