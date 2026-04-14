"""Async client for the Pi-hole v6 REST API."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)



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
class PiholeTopStats:
    top_permitted: list[dict[str, Any]] = field(default_factory=list)
    top_blocked: list[dict[str, Any]] = field(default_factory=list)
    top_clients: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ComponentVersion:
    current: str = ""
    latest: str = ""
    update_available: bool | None = None


@dataclass
class PiholeVersionInfo:
    core: ComponentVersion = field(default_factory=ComponentVersion)
    ftl: ComponentVersion = field(default_factory=ComponentVersion)
    web: ComponentVersion = field(default_factory=ComponentVersion)


AUTH_BACKOFF_SECONDS = 300  # don't retry auth for 5 minutes after a 429


class PiholeClient:
    def __init__(self, url: str, password: str, timeout: float = 10.0):
        self.base_url = url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self._sid: str | None = None
        self._no_auth: bool = False  # True when Pi-hole has no password set
        self._client: httpx.AsyncClient | None = None
        self._auth_blocked_until: float = 0.0
        self._auth_lock: asyncio.Lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the underlying HTTP client. Call once and reuse the instance."""
        self._client = httpx.AsyncClient(timeout=self.timeout, verify=False)

    @property
    def sid(self) -> str | None:
        return self._sid

    @sid.setter
    def sid(self, value: str | None) -> None:
        self._sid = value

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None
        self._sid = None
        self._no_auth = False

    async def __aenter__(self) -> "PiholeClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _authenticate(self) -> bool:
        if self._client is None:
            raise RuntimeError("PiholeClient must be used as an async context manager")
        async with self._auth_lock:
            # Another coroutine may have authenticated while we waited for the lock.
            if self._sid is not None or self._no_auth:
                return True
            now = time.monotonic()
            if now < self._auth_blocked_until:
                remaining = int(self._auth_blocked_until - now)
                logger.debug("Auth blocked for %s — %ds remaining", self.base_url, remaining)
                return False
            try:
                resp = await self._client.post(
                    f"{self.base_url}/api/auth",
                    json={"password": self.password},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    sid = data.get("session", {}).get("sid")
                    if sid:
                        self._sid = sid
                    else:
                        # Pi-hole returned 200 with no SID — password auth is disabled
                        self._no_auth = True
                        logger.debug("No auth required for %s (no password set)", self.base_url)
                    return True
                if resp.status_code == 429:
                    self._auth_blocked_until = time.monotonic() + AUTH_BACKOFF_SECONDS
                    logger.warning(
                        "Rate limited by %s — pausing auth for %ds",
                        self.base_url, AUTH_BACKOFF_SECONDS,
                    )
            except httpx.HTTPError as exc:
                logger.debug("Pi-hole auth failed for %s: %s", self.base_url, exc)
            return False

    def _headers(self) -> dict[str, str]:
        if self._sid:
            return {"X-FTL-SID": self._sid}
        return {}

    async def _ensure_authed(self) -> None:
        if self._sid is None and not self._no_auth:
            ok = await self._authenticate()
            if not ok:
                raise ConnectionError(f"Authentication failed for {self.base_url}")

    async def _get(self, path: str, params: dict | None = None, retry: bool = True) -> Any:
        if self._client is None:
            raise RuntimeError("PiholeClient must be used as an async context manager")
        if self._sid is None and not self._no_auth:
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

    async def _post(self, path: str, json_data: dict | None = None, retry: bool = True) -> Any:
        if self._client is None:
            raise RuntimeError("PiholeClient must be used as an async context manager")
        await self._ensure_authed()
        url = f"{self.base_url}{path}"
        resp = await self._client.post(url, headers=self._headers(), json=json_data)
        if resp.status_code == 401 and retry:
            self._sid = None
            ok = await self._authenticate()
            if ok:
                return await self._post(path, json_data=json_data, retry=False)
            raise ConnectionError(f"Re-authentication failed for {self.base_url}")
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def _delete(self, path: str, retry: bool = True) -> Any:
        if self._client is None:
            raise RuntimeError("PiholeClient must be used as an async context manager")
        await self._ensure_authed()
        url = f"{self.base_url}{path}"
        resp = await self._client.delete(url, headers=self._headers())
        if resp.status_code == 401 and retry:
            self._sid = None
            ok = await self._authenticate()
            if ok:
                return await self._delete(path, retry=False)
            raise ConnectionError(f"Re-authentication failed for {self.base_url}")
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def _get_bytes(self, path: str, retry: bool = True) -> bytes:
        if self._client is None:
            raise RuntimeError("PiholeClient must be used as an async context manager")
        await self._ensure_authed()
        url = f"{self.base_url}{path}"
        resp = await self._client.get(url, headers=self._headers())
        if resp.status_code == 401 and retry:
            self._sid = None
            ok = await self._authenticate()
            if ok:
                return await self._get_bytes(path, retry=False)
            raise ConnectionError(f"Re-authentication failed for {self.base_url}")
        resp.raise_for_status()
        return resp.content

    async def get_teleporter(self) -> bytes:
        """Export full Pi-hole configuration as a zip archive."""
        return await self._get_bytes("/api/teleporter")

    async def post_teleporter(
        self,
        zip_data: bytes,
        import_config: bool = True,
        import_gravity: bool = True,
        import_dhcp_leases: bool = False,
    ) -> None:
        """Import a Pi-hole configuration zip into this instance."""
        if self._client is None:
            raise RuntimeError("PiholeClient must be used as an async context manager")
        await self._ensure_authed()

        import json as _json
        import_payload = _json.dumps({
            "config": import_config,
            "dhcp_leases": import_dhcp_leases,
            "gravity": {
                "group": import_gravity,
                "adlist": import_gravity,
                "adlist_by_group": import_gravity,
                "domain_list": import_gravity,
                "domain_list_by_group": import_gravity,
                "client": import_gravity,
                "client_by_group": import_gravity,
            },
        })

        url = f"{self.base_url}/api/teleporter"
        files = {"file": ("backup.zip", zip_data, "application/zip")}
        data = {"import": import_payload}

        try:
            resp = await self._client.post(url, headers=self._headers(), files=files, data=data)
            if resp.status_code == 401:
                self._sid = None
                ok = await self._authenticate()
                if ok:
                    resp = await self._client.post(url, headers=self._headers(), files=files, data=data)
            resp.raise_for_status()
        except httpx.RemoteProtocolError as exc:
            # Pi-hole FTL restarts itself after importing configuration, which
            # drops the HTTP connection before the response is fully sent.
            # The import succeeded — the incomplete chunked response is expected.
            if "incomplete chunked read" in str(exc).lower():
                logger.info(
                    "Teleporter import to %s: connection reset after import "
                    "(FTL restarted — this is normal, import succeeded).",
                    self.base_url,
                )
            else:
                raise

    async def run_gravity(self) -> None:
        """Trigger a gravity database update on this instance.

        Uses a dedicated throwaway HTTP connection so Pi-hole's inconsistent
        gravity response framing never pollutes the shared persistent client.
        The existing session SID is reused — no new Pi-hole session is created
        and Pi-hole's max_sessions limit is not affected.
        """
        await self._ensure_authed()
        url = f"{self.base_url}/api/action/gravity"

        # Long timeout: gravity can take 30-120 s on a busy instance.
        async with httpx.AsyncClient(timeout=300, verify=False) as tmp:
            try:
                resp = await tmp.post(url, headers=self._headers())
                if resp.status_code == 401:
                    # SID may have expired; re-auth on the persistent client and retry.
                    self._sid = None
                    ok = await self._authenticate()
                    if not ok:
                        raise ConnectionError(f"Re-authentication failed for {self.base_url}")
                    resp = await tmp.post(url, headers=self._headers())
                if resp.status_code >= 400:
                    snippet = resp.text[:200]
                    raise RuntimeError(
                        f"Gravity request to {self.base_url} failed with HTTP {resp.status_code}: {snippet}"
                    )
                # Response body is consumed; the socket closes cleanly with the
                # async-with block, discarding any trailing bytes from Pi-hole's
                # inconsistent HTTP framing without touching self._client.
            except httpx.RemoteProtocolError as exc:
                if "incomplete chunked read" in str(exc).lower():
                    logger.info("Gravity on %s: connection reset (FTL restarted — normal).", self.base_url)
                else:
                    raise

    async def get_version_info(self) -> PiholeVersionInfo:
        """Fetch installed and latest-available versions for core, FTL, and web."""
        data = await self._get("/api/info/version")
        version_root = data.get("version", data)  # handle both wrapped and flat responses

        def _parse_component(raw: dict) -> ComponentVersion:
            local = raw.get("local", raw) or {}
            remote = raw.get("remote", {}) or {}
            current = local.get("version") or local.get("tag") or ""
            # Strip leading 'v' for consistent display
            if current.startswith("v"):
                current = current[1:]
            latest_raw = remote.get("version") or remote.get("tag") or ""
            if latest_raw.startswith("v"):
                latest_raw = latest_raw[1:]
            update_available = raw.get("update_available")
            return ComponentVersion(
                current=current,
                latest=latest_raw,
                update_available=update_available,
            )

        return PiholeVersionInfo(
            core=_parse_component(version_root.get("core", {})),
            ftl=_parse_component(version_root.get("ftl", {})),
            web=_parse_component(version_root.get("web", {})),
        )

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

    async def get_queries(self, from_ts: float | None = None, length: int = 500) -> list[PiholeQuery]:
        """Fetch queries newer than from_ts (Unix timestamp). Returns up to length results."""
        params: dict[str, Any] = {"length": length}
        if from_ts is not None:
            params["from"] = from_ts

        data = await self._get("/api/queries", params=params)
        queries_raw = data.get("queries", []) or []

        queries = []
        for item in queries_raw:
            try:
                ts = datetime.fromtimestamp(item.get("time", 0), tz=timezone.utc)
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
                        status=str(item.get("status", "") or ""),
                        reply_type=reply.get("type", ""),
                        reply_time_ms=reply.get("time", 0.0),
                    )
                )
            except Exception:
                continue

        return queries

    async def block_domain(self, domain: str) -> None:
        """Add domain to the exact deny list on this Pi-hole instance."""
        await self._post(
            "/api/domains/deny/exact",
            json_data={"domain": domain, "comment": "blocked via MyPi", "groups": [0], "enabled": True},
        )

    async def unblock_domain(self, domain: str) -> None:
        """Remove domain from the exact deny list on this Pi-hole instance.

        Pi-hole v6 assigns each deny-list entry a numeric database ID; the DELETE
        endpoint expects that ID, not the domain string.  We GET the list first to
        find the matching entry, then DELETE by ID.  Falls back to the domain-in-path
        form if no id field is present in the response.
        """
        data = await self._get("/api/domains/deny/exact")
        entries = data if isinstance(data, list) else (data.get("domains") or [])
        entry = next((e for e in entries if e.get("domain") == domain), None)
        if entry is None:
            logger.debug("Domain %s not found in exact deny list on %s — nothing to delete", domain, self.base_url)
            return
        entry_id = entry.get("id")
        if entry_id is not None:
            await self._delete(f"/api/domains/deny/exact/{entry_id}")
        else:
            from urllib.parse import quote
            await self._delete(f"/api/domains/deny/exact/{quote(domain, safe='')}")

    async def is_domain_blocked(self, domain: str) -> bool:
        """Return True if domain is present in the exact deny list."""
        try:
            data = await self._get("/api/domains/deny/exact")
            entries = data if isinstance(data, list) else (data.get("domains") or [])
            return any(e.get("domain") == domain for e in entries)
        except Exception:
            return False
