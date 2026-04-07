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
    last_seen_at: datetime | None = None
    status: str = "unknown"  # online / offline / unknown

    # Latest stats
    dns_queries_today: int = 0
    queries_blocked: int = 0
    percent_blocked: float = 0.0
    domains_on_blocklist: int = 0
    unique_clients: int = 0

    model_config = {"from_attributes": True}
