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


class BlockedByListEntry(BaseModel):
    list_id: int | None          # Pi-hole's adlist id; None = unattributed
    name: str                    # human label (list address, or a fallback)
    address: str | None = None   # full adlist URL when known
    is_security: bool = False    # in the configured security/threat group
    count: int


class BlockedByListResponse(BaseModel):
    lists: list[BlockedByListEntry]
    instance_id: uuid.UUID | None = None


class DomainBlocklistEntry(BaseModel):
    """One list that matches a domain, from Pi-hole's live /api/search."""
    name: str                    # display name — adlist URL label, or the pattern
    address: str | None = None   # adlist URL (gravity); None for exact/regex entries
    kind: str                    # "gravity" | "deny-exact" | "deny-regex"
    enabled: bool = True
    is_security: bool = False    # gravity adlist in the configured security group


class DomainBlocklistsResponse(BaseModel):
    """Which list(s) block a single domain — the drill-down card behind clicking
    a blocked domain. Sourced from the master's live /api/search, since Pi-hole's
    per-query list_id doesn't attribute gravity blocks."""
    domain: str
    block_count: int = 0         # times gravity-blocked in the window (context)
    lists: list[DomainBlocklistEntry]
