from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class InstanceStatus(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    color: str
    is_active: bool
    is_master: bool = False
    last_seen_at: datetime | None = None
    status: str = "unknown"  # online / offline / unknown

    # Latest stats
    dns_queries_today: int = 0
    queries_blocked: int = 0
    percent_blocked: float = 0.0
    domains_on_blocklist: int = 0
    unique_clients: int = 0

    model_config = {"from_attributes": True}


class ComponentVersionSchema(BaseModel):
    current: str = ""
    latest: str = ""
    update_available: bool | None = None


class InstanceVersionInfo(BaseModel):
    id: uuid.UUID
    name: str
    color: str
    is_master: bool = False
    core: ComponentVersionSchema = Field(default_factory=ComponentVersionSchema)
    ftl: ComponentVersionSchema = Field(default_factory=ComponentVersionSchema)
    web: ComponentVersionSchema = Field(default_factory=ComponentVersionSchema)
    error: str | None = None
