"""Async client for the Pi-hole v6 REST API."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Pi-hole v6 query status labels
QUERY_STATUS = {
    1: "blocked_gravity",
    2: "forwarded",
    3: "cached",
    4: "blocked_regex",
    5: "blocked_denylist",
    6: "blocked_nxdomain",
    7: "blocked_cname_gravity",
    8: "blocked_cname_regex",
    9: "blocked_cname_denylist",
    10: "allowed_retried",
    11: "allowed_retried_dnssec",
    12: "allowed_special_domain",
    13: "blocked_gravity_cname",
    14: "allowed_unknown",
}


@dataclass
class PiholeSummary:
    dns_queries_today: int = 0
    queries_blocked: int = 0
    percent_blocked: float = 0.0
    domains_on_blocklist: int = 0
    unique_clients: int = 0
    queries_cached: int = 0
    queries_forwarded: int = 0


@dataclass
class PiholeQuery:
    pihole_id: str
    timestamp: datetime
    query_type: str
    domain: str
    client_ip: str
    client_name: str
    status: str
    reply_type: str
    reply_time_ms: float


@dataclass
class PiholeHistoryBucket:
    timestamp: datetime
    queries: int
    blocked: int


@dataclass
class PiholeTopStats:
    top_permitted: list[dict[str, Any]] = field(default_factory=list)
    top_blocked: list[dict[str, Any]] = field(default_factory=list)
    top_clients: list[dict[str, Any]] = field(default_factory=list)


class PiholeClient:
    def __init__(self, url: str, password: str, timeout: float = 10.0):
        self.base_url = url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self._sid: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "PiholeClient":
        self._client = httpx.AsyncClient(timeout=self.timeout, verify=False)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None
        self._sid = None

    async def _authenticate(self) -> bool:
        if self._client is None:
            raise RuntimeError("PiholeClient must be used as an async context manager")
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/auth",
                json={"password": self.password},
            )
            if resp.status_code == 200:
                data = resp.json()
                self._sid = data.get("session", {}).get("sid")
                return self._sid is not None
        except httpx.HTTPError as exc:
            logger.debug("Pi-hole auth failed for %s: %s", self.base_url, exc)
        return False

    def _headers(self) -> dict[str, str]:
        if self._sid:
            return {"X-FTL-SID": self._sid}
        return {}

    async def _get(self, path: str, params: dict | None = None, retry: bool = True) -> Any:
        if self._client is None:
            raise RuntimeError("PiholeClient must be used as an async context manager")
        if self._sid is None:
            ok = await self._authenticate()
            if not ok:
                raise ConnectionError(f"Authentication failed for {self.base_url}")

        url = f"{self.base_url}{path}"
        resp = await self._client.get(url, headers=self._headers(), params=params)

        if resp.status_code == 401 and retry:
            self._sid = None
            ok = await self._authenticate()
            if ok:
                return await self._get(path, params=params, retry=False)
            raise ConnectionError(f"Re-authentication failed for {self.base_url}")

        resp.raise_for_status()
        return resp.json()

    async def get_summary(self) -> PiholeSummary:
        data = await self._get("/api/stats/summary")
        q = data.get("queries", {})
        return PiholeSummary(
            dns_queries_today=q.get("total", 0),
            queries_blocked=q.get("blocked", 0),
            percent_blocked=q.get("percent_blocked", 0.0),
            domains_on_blocklist=data.get("gravity", {}).get("domains_being_blocked", 0),
            unique_clients=data.get("clients", {}).get("active", 0),
            queries_cached=q.get("cached", 0),
            queries_forwarded=q.get("forwarded", 0),
        )

    async def get_history(self) -> list[PiholeHistoryBucket]:
        """Return over-time data in 10-minute buckets."""
        data = await self._get("/api/stats/history")
        buckets = []
        history = data.get("history", [])
        for item in history:
            ts = datetime.fromtimestamp(item.get("timestamp", 0), tz=timezone.utc)
            buckets.append(
                PiholeHistoryBucket(
                    timestamp=ts,
                    queries=item.get("total", 0),
                    blocked=item.get("blocked", 0),
                )
            )
        return buckets

    async def get_top_stats(self, count: int = 10) -> PiholeTopStats:
        permitted_data = await self._get("/api/stats/top_domains", params={"blocked": "false", "count": count})
        blocked_data = await self._get("/api/stats/top_domains", params={"blocked": "true", "count": count})
        clients_data = await self._get("/api/stats/top_clients", params={"count": count})

        top_permitted = [
            {"domain": domain, "count": cnt}
            for domain, cnt in (permitted_data.get("domains", {}) or {}).items()
        ]
        top_blocked = [
            {"domain": domain, "count": cnt}
            for domain, cnt in (blocked_data.get("domains", {}) or {}).items()
        ]
        top_clients = []
        for item in clients_data.get("clients", []) or []:
            top_clients.append({
                "client": item.get("name") or item.get("ip", ""),
                "count": item.get("count", 0),
            })

        return PiholeTopStats(
            top_permitted=sorted(top_permitted, key=lambda x: x["count"], reverse=True),
            top_blocked=sorted(top_blocked, key=lambda x: x["count"], reverse=True),
            top_clients=sorted(top_clients, key=lambda x: x["count"], reverse=True),
        )

    async def get_queries(self, cursor: str | None = None, length: int = 500) -> tuple[list[PiholeQuery], str | None]:
        """Fetch a page of queries. Returns (queries, next_cursor)."""
        params: dict[str, Any] = {"length": length}
        if cursor:
            params["cursor"] = cursor

        data = await self._get("/api/queries", params=params)
        queries_raw = data.get("queries", []) or []
        next_cursor = data.get("cursor")

        queries = []
        for item in queries_raw:
            try:
                ts = datetime.fromtimestamp(item.get("time", 0), tz=timezone.utc)
                status_int = item.get("status", 0)
                status_str = QUERY_STATUS.get(status_int, str(status_int))
                reply = item.get("reply", {}) or {}
                client = item.get("client", {}) or {}
                queries.append(
                    PiholeQuery(
                        pihole_id=str(item.get("id", "")),
                        timestamp=ts,
                        query_type=item.get("type", ""),
                        domain=item.get("domain", ""),
                        client_ip=client.get("ip", ""),
                        client_name=client.get("name", ""),
                        status=status_str,
                        reply_type=reply.get("type", ""),
                        reply_time_ms=reply.get("time", 0.0),
                    )
                )
            except Exception:
                continue

        return queries, next_cursor
