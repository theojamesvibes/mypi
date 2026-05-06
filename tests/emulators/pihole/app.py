"""Stateless Pi-hole v6 REST emulator for end-to-end MyPi tests.

This is a tiny FastAPI app that mimics just enough of the Pi-hole v6
API for the MyPi collector to talk to it: auth, summary stats, queries,
top-domains/clients, version info, deny/allow lists. Responses are
deterministic functions of the wall clock so polling produces stable
data while still exercising the live HTTP path (httpx, SID header,
401/retry, etc.) end-to-end.

Why stateless: blocking a domain via /api/domains/deny/exact does *not*
affect the queries returned next poll. Stateful behaviour (block →
list → unblock loop) can be added later if a test needs it; the v1
suite only asserts on the polling pipeline, not the block flow.

Auth model:
  POST /api/auth        → 200 {"session": {"sid": "<random>", "valid": true}}
  All other endpoints   → require X-FTL-SID header matching the live SID,
                          else 401. This exercises the client's
                          re-authentication retry path.

Pi-hole v6 actually returns 200 with sid=null when the password is
empty (no-auth mode). We don't emulate that path here — the tests
configure passwords, so the SID flow is what matters.
"""
from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse


PASSWORD = os.getenv("EMULATOR_PIHOLE_PASSWORD", "emulator-password")
INSTANCE_NAME = os.getenv("EMULATOR_INSTANCE_NAME", "emu-pihole")

# In-memory session table. Pi-hole's webserver caps concurrent sessions
# at `webserver.api.max_sessions` (default 16); we allow up to 32 here
# so a flaky test loop that re-auths a few times doesn't trip the cap.
_sessions: dict[str, float] = {}
_MAX_SESSIONS = 32
_SESSION_TTL_SECONDS = 3600


def _new_sid() -> str:
    # 36 chars of urlsafe base64 — comfortably more entropy than what
    # Pi-hole uses, indistinguishable for the client which just stores it.
    return secrets.token_urlsafe(27)


def _check_sid(x_ftl_sid: str | None) -> None:
    """Raise 401 unless the supplied SID is in the live session table.

    Mirrors Pi-hole's behaviour after a webserver restart or session
    timeout — the persisted SID we replayed from app_settings will fail
    here on cold-start, MyPi's client falls back to /api/auth, and the
    new SID gets persisted for next time.
    """
    if not x_ftl_sid or x_ftl_sid not in _sessions:
        raise HTTPException(status_code=401, detail="Unauthorized")
    # Refresh TTL on use — keeps long polls alive without re-auth churn.
    _sessions[x_ftl_sid] = time.time() + _SESSION_TTL_SECONDS


app = FastAPI(title="Pi-hole v6 Emulator", docs_url=None, redoc_url=None)


@app.post("/api/auth")
async def auth(request: Request):
    body = await request.json()
    if body.get("password") != PASSWORD:
        # Real Pi-hole returns 401 with {"session": {"valid": false, ...}}
        # but the MyPi client only checks status_code so a bare 401 works.
        raise HTTPException(status_code=401, detail="Bad password")

    # Evict the oldest session if we'd otherwise exceed the cap. Reflects
    # what Pi-hole would do when max_sessions is hit, except we don't
    # surface 429 — tests don't care about the exact error path.
    if len(_sessions) >= _MAX_SESSIONS:
        oldest = min(_sessions, key=_sessions.get)  # type: ignore[arg-type]
        _sessions.pop(oldest, None)

    sid = _new_sid()
    _sessions[sid] = time.time() + _SESSION_TTL_SECONDS
    return {
        "session": {
            "sid": sid,
            "valid": True,
            "totp": False,
            "csrf": secrets.token_urlsafe(16),
            "validity": _SESSION_TTL_SECONDS,
        }
    }


@app.delete("/api/auth")
async def logout(x_ftl_sid: str | None = Header(default=None)):
    if x_ftl_sid:
        _sessions.pop(x_ftl_sid, None)
    return JSONResponse(status_code=204, content=None)


@app.get("/api/info/version")
async def info_version(x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    # Shape matches what the v6 client's `_parse_component` expects:
    # nested local/remote with version + tag, plus update_available.
    def _component(local_v: str, remote_v: str, update: bool) -> dict[str, Any]:
        return {
            "local": {"version": local_v, "tag": local_v, "branch": "master"},
            "remote": {"version": remote_v, "tag": remote_v},
            "update_available": update,
        }

    return {
        "version": {
            "core": _component("v6.0.0", "v6.0.0", False),
            "ftl": _component("v6.0.0", "v6.0.1", True),
            "web": _component("v6.0.0", "v6.0.0", False),
        }
    }


@app.get("/api/stats/summary")
async def stats_summary(x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    # Numbers are deterministic — the collector polls at ~5s intervals
    # in tests, so emitting wall-clock-derived numbers would give the
    # asserter a moving target. Fixed values here; stats history fans
    # out on the dashboard from the snapshots MyPi writes per poll.
    return {
        "queries": {
            "total": 5000,
            "blocked": 1250,
            "percent_blocked": 25.0,
            "unique_domains": 600,
            "forwarded": 3000,
            "cached": 750,
            "frequency": 1.5,
            "types": {"A": 4200, "AAAA": 800},
            "status": {"FORWARDED": 3000, "GRAVITY": 1250, "CACHE": 750},
            "replies": {"IP": 4500, "NXDOMAIN": 250, "CNAME": 250},
        },
        "clients": {"active": 8, "total": 12},
        "gravity": {
            "domains_being_blocked": 175000,
            "last_update": int(time.time()) - 3600,
        },
    }


@app.get("/api/queries")
async def queries(
    x_ftl_sid: str | None = Header(default=None),
    # Real Pi-hole's /api/queries accepts `length` up to 100k; the
    # MyPi backfill calls this with length=10000 per hourly window
    # so anything below that 422s and aborts the entire backfill.
    length: int = Query(default=100, ge=1, le=100000),
    from_: float | None = Query(default=None, alias="from"),
    until: float | None = Query(default=None),
):
    _check_sid(x_ftl_sid)
    # Generate a fixed set of synthetic queries timestamped relative to
    # `now`. Rows are emitted newest-first (i=0 is wall-clock now,
    # i=59 is now-360s). The from_ / until filters apply to the
    # synthetic timeline, mirroring real Pi-hole behaviour.
    now = time.time()
    domains_blocked = ["ads.emu.test", "tracker.emu.test", "telemetry.emu.test"]
    domains_ok = ["api.emu.test", "cdn.emu.test", "www.emu.test", "mail.emu.test"]
    clients = [
        ("192.168.99.10", "laptop.emu.test"),
        ("192.168.99.20", "phone.emu.test"),
        ("192.168.99.30", "tv.emu.test"),
    ]

    rows: list[dict[str, Any]] = []
    # Yield up to `length` rows: ~30 % blocked, deterministic mix.
    for i in range(min(length, 60)):
        ts = now - (i * 6)  # one query every 6 seconds going back ~6 min
        # Newest-first iteration: once ts drops below from_, every
        # remaining row is older and can be skipped.
        if from_ is not None and ts < from_:
            break
        # Skip rows newer than `until`. We don't break here because the
        # next iteration's ts is older — it may fall back inside the
        # window. (For the collector's hourly backfill windows the
        # synthetic 6-min span lands entirely in the most recent
        # window anyway, but the filter is cheap to apply correctly.)
        if until is not None and ts > until:
            continue
        is_blocked = (i % 3) == 0
        domain = (
            domains_blocked[i % len(domains_blocked)] if is_blocked
            else domains_ok[i % len(domains_ok)]
        )
        client_ip, client_name = clients[i % len(clients)]
        rows.append({
            "id": int(now * 1000) - i,
            "time": ts,
            "type": "A",
            "domain": domain,
            "client": {"ip": client_ip, "name": client_name},
            "status": "GRAVITY" if is_blocked else "FORWARDED",
            "reply": {
                "type": "NXDOMAIN" if is_blocked else "IP",
                "time": 1.2 if is_blocked else 18.7,
            },
            "dnssec": "INSECURE",
            "ede": {"code": -1, "text": ""},
        })

    return {
        "queries": rows,
        "cursor": None,
        "recordsTotal": len(rows),
        "recordsFiltered": len(rows),
        "draw": 1,
        "took": 0.001,
    }


@app.get("/api/stats/top_domains")
async def top_domains(
    x_ftl_sid: str | None = Header(default=None),
    blocked: bool = False,
    count: int = Query(default=10, ge=1, le=100),
):
    _check_sid(x_ftl_sid)
    if blocked:
        return {
            "domains": [
                {"domain": "ads.emu.test", "count": 420},
                {"domain": "tracker.emu.test", "count": 380},
                {"domain": "telemetry.emu.test", "count": 200},
            ][:count],
            "total_queries": 1250,
            "blocked_queries": 1250,
        }
    return {
        "domains": [
            {"domain": "api.emu.test", "count": 1100},
            {"domain": "cdn.emu.test", "count": 850},
            {"domain": "www.emu.test", "count": 600},
            {"domain": "mail.emu.test", "count": 200},
        ][:count],
        "total_queries": 5000,
        "blocked_queries": 1250,
    }


@app.get("/api/stats/top_clients")
async def top_clients(
    x_ftl_sid: str | None = Header(default=None),
    blocked: bool = False,
    count: int = Query(default=10, ge=1, le=100),
):
    _check_sid(x_ftl_sid)
    return {
        "clients": [
            {"ip": "192.168.99.10", "name": "laptop.emu.test", "count": 2200},
            {"ip": "192.168.99.20", "name": "phone.emu.test", "count": 1800},
            {"ip": "192.168.99.30", "name": "tv.emu.test", "count": 1000},
        ][:count],
        "total_queries": 5000,
        "blocked_queries": 1250,
    }


# ── Domain deny/allow lists (stateful) ──────────────────────────────────────
#
# These endpoints back the block/unblock UI flow. Real Pi-hole stores these
# in its config DB; we keep an in-memory dict per kind so the test can
# block a domain, see it appear in the GET listing, then unblock and see
# it disappear. State is process-wide and resets when the emulator
# restarts (between test sessions).
_deny_list: dict[str, dict[str, Any]] = {}
_allow_list: dict[str, dict[str, Any]] = {}


def _list_for(kind: str) -> dict[str, dict[str, Any]]:
    return _deny_list if kind == "deny" else _allow_list


def _entries(store: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the list in the shape MyPi's pihole_client._present expects:
    a list of objects with a `domain` field."""
    return [{"domain": d, **meta} for d, meta in store.items()]


@app.get("/api/domains/deny/exact")
async def deny_list(x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    return {"domains": _entries(_deny_list)}


@app.get("/api/domains/allow/exact")
async def allow_list(x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    return {"domains": _entries(_allow_list)}


async def _add_to_list(store: dict[str, dict[str, Any]], request: Request) -> dict[str, Any]:
    body = await request.json()
    domain = body.get("domain", "").strip().lower()
    if not domain:
        raise HTTPException(status_code=422, detail="domain is required")
    # Real Pi-hole returns 409 on duplicate; MyPi's add_*_exact handles
    # that as a no-op so this preserves the idempotent semantics.
    if domain in store:
        raise HTTPException(status_code=409, detail="domain already in list")
    store[domain] = {
        "comment": body.get("comment"),
        "enabled": body.get("enabled", True),
        "groups": body.get("groups", [0]),
    }
    return {"processed": {"success": [{"item": domain}], "errors": []}}


@app.post("/api/domains/deny/exact")
async def add_deny(request: Request, x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    return await _add_to_list(_deny_list, request)


@app.post("/api/domains/allow/exact")
async def add_allow(request: Request, x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    return await _add_to_list(_allow_list, request)


@app.delete("/api/domains/deny/exact/{domain:path}")
async def remove_deny(domain: str, x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    _deny_list.pop(domain.lower(), None)
    return JSONResponse(status_code=204, content=None)


@app.delete("/api/domains/allow/exact/{domain:path}")
async def remove_allow(domain: str, x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    _allow_list.pop(domain.lower(), None)
    return JSONResponse(status_code=204, content=None)


# ── Reset hook (test convenience) ───────────────────────────────────────────


@app.post("/__test__/reset")
async def reset_emulator():
    """Wipe in-memory state. Called by the conftest between tests so
    the deny/allow lists and sync counters don't leak across cases."""
    _deny_list.clear()
    _allow_list.clear()
    _sync_state["gravity_runs"] = 0
    _sync_state["teleporter_imports"] = 0
    _sync_state["last_imported_size"] = 0
    return {"reset": True}


# ── Sync support (stateful) ─────────────────────────────────────────────────
#
# Master path: GET /api/teleporter returns a minimal-but-valid ZIP, and
# POST /api/action/gravity records that gravity ran.
# Replica path: POST /api/teleporter records that an import landed and
# stores the body for the test to assert on.
#
# Real Pi-hole's teleporter ZIP is a structured archive; MyPi only
# validates the ZIP header + central directory before forwarding it,
# so we ship an empty-but-valid ZIP and let the replica accept it.

_sync_state: dict[str, Any] = {
    "gravity_runs": 0,
    "teleporter_imports": 0,
    "last_imported_size": 0,
}


def _make_minimal_zip() -> bytes:
    """Smallest valid ZIP. MyPi's `_validate_teleporter_zip` only checks
    the magic header + central directory shape; an empty ZIP passes.
    """
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # One trivial file so the central directory isn't completely empty.
        zf.writestr("etc/pihole/setupVars.conf", b"# emulator export\n")
    return buf.getvalue()


@app.get("/api/teleporter")
async def export_teleporter(x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    from fastapi.responses import Response
    return Response(
        content=_make_minimal_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=teleporter.zip"},
    )


@app.post("/api/teleporter")
async def import_teleporter(request: Request, x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    body = await request.body()
    _sync_state["teleporter_imports"] += 1
    _sync_state["last_imported_size"] = len(body)
    return {"processed": {"success": True, "errors": []}}


@app.post("/api/action/gravity")
async def run_gravity(x_ftl_sid: str | None = Header(default=None)):
    _check_sid(x_ftl_sid)
    _sync_state["gravity_runs"] += 1
    return {"processed": {"success": True}}


@app.get("/__test__/sync_state")
async def get_sync_state():
    """Read-only test introspection — what calls has the emulator received?"""
    return dict(_sync_state)


@app.get("/api/health")
async def health():
    """Unauthenticated readiness endpoint used by the test fixture."""
    return {"status": "ok", "instance": INSTANCE_NAME, "live_sessions": len(_sessions)}
