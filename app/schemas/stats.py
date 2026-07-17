"""Response shapes for the /api/stats endpoints.

These define the JSON the dashboard and iOS app receive — they are not
database tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class SummaryStats(BaseModel):
    dns_queries_today: int
    queries_blocked: int
    percent_blocked: float
    domains_on_blocklist: int
    unique_clients: int
    queries_cached: int
    queries_forwarded: int


class AggregatedSummary(BaseModel):
    totals: SummaryStats
    instances: list[dict]   # per-instance stats


class HistoryBucket(BaseModel):
    timestamp: datetime
    queries: int
    blocked: int


class HistoryResponse(BaseModel):
    buckets: list[HistoryBucket]
    instance_id: uuid.UUID | None = None  # None = aggregated


class TopDomain(BaseModel):
    domain: str
    count: int


class TopClient(BaseModel):
    client: str
    count: int


class TopStatsResponse(BaseModel):
    top_permitted: list[TopDomain]
    top_blocked: list[TopDomain]
    top_clients: list[TopClient]
    instance_id: uuid.UUID | None = None
