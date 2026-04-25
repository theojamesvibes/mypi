from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class InstanceStatus(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    color: str
    is_active: bool
    is_master: bool = False
    # None / "master" / "replica" — VIP cluster membership; surfaced for the
    # systems-table pill in the dashboard.
    vip_role: str | None = None
    last_seen_at: datetime | None = None
    status: str = "unknown"  # online / offline / unknown

    # Latest stats
    dns_queries_today: int = 0
    queries_blocked: int = 0
    percent_blocked: float = 0.0
    domains_on_blocklist: int = 0
    unique_clients: int = 0

    # Software versions (persisted from last successful poll)
    version_core: str | None = None
    version_ftl: str | None = None
    version_web: str | None = None
    update_available_core: bool | None = None
    update_available_ftl: bool | None = None
    update_available_web: bool | None = None

    model_config = {"from_attributes": True}
