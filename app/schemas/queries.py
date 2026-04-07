from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class QueryLogEntry(BaseModel):
    id: uuid.UUID
    instance_id: uuid.UUID
    instance_name: str = ""
    timestamp: datetime
    client_ip: str | None = None
    client_name: str | None = None
    query_type: str | None = None
    domain: str | None = None
    status: str | None = None
    reply_type: str | None = None
    reply_time_ms: float | None = None

    model_config = {"from_attributes": True}


class QueryLogPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[QueryLogEntry]
