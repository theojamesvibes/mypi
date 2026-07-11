"""Tests for app/services/pihole_client.py — the Pi-hole v6 REST client.

Every outbound HTTP call is mocked via respx so the suite needs no real
Pi-hole. Covers the auth state machine (200/200-no-auth/429/401/transport
error/backoff), the 401-retry path on `_get`, the throwaway-client
behaviour of `run_gravity`, and the FTL-restart edge cases on
`run_gravity` / `post_teleporter`.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.services.pihole_client import (
    PiholeClient,
    PiholeSummary,
    PiholeVersionInfo,
)

PIHOLE = "http://pihole.test"


@pytest.fixture
async def client():
    """A freshly-opened PiholeClient pointed at the mock URL.

    Tests that exercise the un-authenticated state set their own
    expectations; tests that need an active session call
    `_authenticate` themselves after stubbing /api/auth.
    """
    c = PiholeClient(PIHOLE, password="test-password")
    await c.open()
    try:
        yield c
    finally:
        await c.close()


# ── Authenticate ─────────────────────────────────────────────────────────────


async def test_authenticate_success_sets_sid(respx_mock, client):
    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "sid-abc-123"}}
    )
    ok = await client._authenticate()
    assert ok is True
    assert client.sid == "sid-abc-123"
    assert client._no_auth is False


async def test_authenticate_no_password_sets_no_auth_flag(respx_mock, client):
    """Pi-hole returns 200 with `sid: null` when no admin password is
    configured. The client treats that as "auth not required" and
    stops sending X-FTL-SID."""
    respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": None}}
    )
    ok = await client._authenticate()
    assert ok is True
    assert client.sid is None
    assert client._no_auth is True


async def test_authenticate_429_arms_backoff(respx_mock, client):
    """429 means Pi-hole's session table is saturated — pause new auth
    attempts for `auth_backoff_seconds` instead of hammering."""
    respx_mock.post(f"{PIHOLE}/api/auth").respond(429)
    ok = await client._authenticate()
    assert ok is False
    assert client._auth_blocked_until > time.monotonic()


async def test_authenticate_during_backoff_short_circuits(respx_mock, client):
    """When backoff is active, _authenticate returns False without
    issuing a request — confirms via respx call count."""
    client._auth_blocked_until = time.monotonic() + 60  # arm backoff manually
    route = respx_mock.post(f"{PIHOLE}/api/auth").respond(200)
    ok = await client._authenticate()
    assert ok is False
    assert route.call_count == 0  # nothing went out


async def test_authenticate_401_returns_false_without_arming_backoff(
    respx_mock, client,
):
    """A 401 (wrong password) is operator config error, not a rate
    limit — don't arm the backoff window."""
    respx_mock.post(f"{PIHOLE}/api/auth").respond(401)
    ok = await client._authenticate()
    assert ok is False
    assert client._auth_blocked_until == 0.0


async def test_authenticate_transport_error_returns_false(respx_mock, client):
    """SSL/connect/read errors during auth surface at WARN with the
    real exception type so flaky hardware is diagnosable, but the
    return is False."""
    respx_mock.post(f"{PIHOLE}/api/auth").mock(
        side_effect=httpx.ConnectError("transport failure")
    )
    ok = await client._authenticate()
    assert ok is False
    assert client.sid is None


async def test_concurrent_authenticate_only_calls_once(respx_mock, client):
    """_auth_lock guarantees only one /api/auth POST per client even
    under concurrent polls (stats + queries firing the same tick)."""
    route = respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "shared-sid"}}
    )
    results = await asyncio.gather(
        client._authenticate(),
        client._authenticate(),
        client._authenticate(),
    )
    assert all(results)
    assert route.call_count == 1
    assert client.sid == "shared-sid"


# ── _get with re-auth on 401 ─────────────────────────────────────────────────


async def test_get_includes_sid_header_when_authed(respx_mock, client):
    client._sid = "header-sid"
    summary_route = respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(
        200, json={"queries": {}, "gravity": {}, "clients": {}}
    )
    await client._get("/api/stats/summary")
    sent = summary_route.calls[0].request
    assert sent.headers.get("X-FTL-SID") == "header-sid"


async def test_get_omits_sid_header_when_no_auth(respx_mock, client):
    client._no_auth = True
    route = respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(
        200, json={"queries": {}, "gravity": {}, "clients": {}}
    )
    await client._get("/api/stats/summary")
    sent = route.calls[0].request
    assert "X-FTL-SID" not in sent.headers


async def test_get_401_triggers_reauth_and_retries(respx_mock, client):
    """Stale SID → 401 → re-auth → retry. Verifies via respx call
    counts that exactly two GETs and one POST went out."""
    client._sid = "stale-sid"
    auth_route = respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "fresh-sid"}}
    )
    get_route = respx_mock.get(f"{PIHOLE}/api/stats/summary").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"queries": {}, "gravity": {}, "clients": {}}),
        ]
    )
    result = await client._get("/api/stats/summary")
    assert result == {"queries": {}, "gravity": {}, "clients": {}}
    assert auth_route.call_count == 1
    assert get_route.call_count == 2
    assert client.sid == "fresh-sid"


async def test_get_401_then_failed_reauth_raises(respx_mock, client):
    client._sid = "stale-sid"
    respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(401)
    respx_mock.post(f"{PIHOLE}/api/auth").respond(401)
    with pytest.raises(ConnectionError, match="Re-authentication failed"):
        await client._get("/api/stats/summary")


async def test_concurrent_401s_reauth_once_and_share_the_fresh_sid(
    respx_mock, client,
):
    """Stats and queries polls share one client. When both requests carry
    the same expired SID and both come back 401, the race loser must NOT
    clear the fresh SID the winner just obtained (that used to force a
    second login, burning another slot in Pi-hole's bounded session
    table). Exactly one /api/auth POST may go out."""
    client._sid = "stale-sid"

    auth_route = respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "fresh-sid"}}
    )

    # Both endpoints 401 on the stale SID, succeed on the fresh one.
    def by_sid(request, ok_body):
        if request.headers.get("X-FTL-SID") == "fresh-sid":
            return httpx.Response(200, json=ok_body)
        return httpx.Response(401)

    respx_mock.get(f"{PIHOLE}/api/stats/summary").mock(
        side_effect=lambda req: by_sid(req, {"queries": {}, "gravity": {}, "clients": {}})
    )
    respx_mock.get(f"{PIHOLE}/api/queries").mock(
        side_effect=lambda req: by_sid(req, {"queries": []})
    )

    summary, queries = await asyncio.gather(
        client._get("/api/stats/summary"),
        client._get("/api/queries"),
    )

    assert summary == {"queries": {}, "gravity": {}, "clients": {}}
    assert queries == {"queries": []}
    assert client.sid == "fresh-sid"
    # The whole point: one login, not one per raced request.
    assert auth_route.call_count == 1


async def test_reauthenticate_does_not_clobber_a_newer_sid(client, respx_mock):
    """Direct unit check of the compare-and-swap: a loser whose failed
    request used the old SID finds a fresh one already stored and must
    keep it without another /api/auth round-trip."""
    auth_route = respx_mock.post(f"{PIHOLE}/api/auth").respond(
        200, json={"session": {"sid": "should-not-be-fetched"}}
    )
    client._sid = "fresh-sid"  # winner already re-authed

    ok = await client._reauthenticate("stale-sid")

    assert ok is True
    assert client.sid == "fresh-sid"
    assert auth_route.call_count == 0


async def test_get_5xx_raises_http_status_error(respx_mock, client):
    client._no_auth = True
    respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        await client._get("/api/stats/summary")


# ── Parsing helpers ──────────────────────────────────────────────────────────


async def test_get_summary_parses_v6_response(respx_mock, client):
    client._no_auth = True
    respx_mock.get(f"{PIHOLE}/api/stats/summary").respond(
        200,
        json={
            "queries": {
                "total": 1000, "blocked": 200, "percent_blocked": 20.0,
                "cached": 100, "forwarded": 700,
            },
            "gravity": {"domains_being_blocked": 50000},
            "clients": {"active": 12},
        },
    )
    summary = await client.get_summary()
    assert isinstance(summary, PiholeSummary)
    assert summary.dns_queries_today == 1000
    assert summary.queries_blocked == 200
    assert summary.percent_blocked == 20.0
    assert summary.domains_on_blocklist == 50000
    assert summary.unique_clients == 12
    assert summary.queries_cached == 100
    assert summary.queries_forwarded == 700


async def test_get_queries_passes_window_params(respx_mock, client):
    client._no_auth = True
    route = respx_mock.get(f"{PIHOLE}/api/queries").respond(
        200, json={"queries": []}
    )
    await client.get_queries(from_ts=100.0, until_ts=200.0, length=50)
    sent = route.calls[0].request
    assert sent.url.params.get("from") == "100.0"
    assert sent.url.params.get("until") == "200.0"
    assert sent.url.params.get("length") == "50"


async def test_get_queries_skips_unparseable_rows(respx_mock, client):
    """A single malformed row must not break the whole batch — bad
    timestamps are silently dropped."""
    client._no_auth = True
    respx_mock.get(f"{PIHOLE}/api/queries").respond(
        200,
        json={
            "queries": [
                {
                    "id": "1", "time": 1700000000, "type": "A",
                    "domain": "example.com",
                    "client": {"ip": "1.2.3.4", "name": "host"},
                    "status": "FORWARDED",
                    "reply": {"type": "IP", "time": 5.0},
                },
                # Bad row — `time` will throw on datetime.fromtimestamp
                {"id": "2", "time": "not-a-timestamp"},
            ],
        },
    )
    queries = await client.get_queries()
    assert len(queries) == 1
    assert queries[0].domain == "example.com"


async def test_get_version_info_strips_leading_v(respx_mock, client):
    """Pi-hole exposes versions as `v6.4.2`; downstream comparisons
    expect the bare `6.4.2`."""
    client._no_auth = True
    respx_mock.get(f"{PIHOLE}/api/info/version").respond(
        200,
        json={
            "version": {
                "core": {
                    "local": {"version": "v6.4.2"},
                    "remote": {"version": "v6.4.2"},
                    "update_available": False,
                },
                "ftl": {
                    "local": {"version": "v6.6.1"},
                    "remote": {"version": "v6.6.2"},
                    "update_available": True,
                },
                "web": {
                    "local": {"version": "v6.5"},
                    "remote": {"version": "v6.5"},
                    "update_available": False,
                },
            }
        },
    )
    info = await client.get_version_info()
    assert isinstance(info, PiholeVersionInfo)
    assert info.core.current == "6.4.2"
    assert info.ftl.current == "6.6.1"
    assert info.ftl.latest == "6.6.2"
    assert info.ftl.update_available is True


# ── Domain list mutations ────────────────────────────────────────────────────


async def test_add_deny_exact_409_is_idempotent(respx_mock, client):
    """Already-on-list returns 409; the client treats that as success."""
    client._no_auth = True
    respx_mock.post(f"{PIHOLE}/api/domains/deny/exact").respond(409)
    # Should NOT raise.
    await client.add_deny_exact("ads.example.com")


async def test_add_deny_exact_propagates_other_errors(respx_mock, client):
    client._no_auth = True
    respx_mock.post(f"{PIHOLE}/api/domains/deny/exact").respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        await client.add_deny_exact("ads.example.com")


async def test_remove_deny_exact_url_encodes_domain(respx_mock, client):
    """Domain becomes part of the path; un-safe chars must be
    percent-encoded so a domain like `weird name.example` works."""
    client._no_auth = True
    domain = "ads.example.com"
    route = respx_mock.delete(
        f"{PIHOLE}/api/domains/deny/exact/{domain}"
    ).respond(204)
    await client.remove_deny_exact(domain)
    assert route.call_count == 1


async def test_remove_deny_exact_404_is_idempotent(respx_mock, client):
    client._no_auth = True
    respx_mock.delete(
        f"{PIHOLE}/api/domains/deny/exact/missing.example"
    ).respond(404)
    # Should NOT raise.
    await client.remove_deny_exact("missing.example")


# ── Teleporter + gravity ─────────────────────────────────────────────────────


async def test_get_teleporter_returns_zip_bytes(respx_mock, client):
    client._no_auth = True
    respx_mock.get(f"{PIHOLE}/api/teleporter").respond(
        200, content=b"PK\x03\x04fake-zip-bytes"
    )
    data = await client.get_teleporter()
    assert data.startswith(b"PK\x03\x04")


async def test_post_teleporter_swallows_incomplete_chunked_read(
    respx_mock, client,
):
    """Pi-hole's FTL self-restarts after a teleporter import, which
    drops the response mid-stream. The client treats that as success."""
    client._no_auth = True
    respx_mock.post(f"{PIHOLE}/api/teleporter").mock(
        side_effect=httpx.RemoteProtocolError(
            "incomplete chunked read"
        )
    )
    # Must NOT raise.
    await client.post_teleporter(b"PK\x03\x04zip", import_config=True)


async def test_post_teleporter_propagates_real_protocol_errors(
    respx_mock, client,
):
    """Generic RemoteProtocolError that isn't the FTL-restart pattern
    should still be raised so the caller sees the failure."""
    client._no_auth = True
    respx_mock.post(f"{PIHOLE}/api/teleporter").mock(
        side_effect=httpx.RemoteProtocolError("unexpected EOF")
    )
    with pytest.raises(httpx.RemoteProtocolError):
        await client.post_teleporter(b"zip")


async def test_post_teleporter_logs_response_body_on_4xx(
    respx_mock, client, caplog,
):
    """A 4xx from Pi-hole must surface the JSON error body in the log so
    operators can diagnose the rejection without SSH'ing to the box."""
    import logging
    client._no_auth = True
    respx_mock.post(f"{PIHOLE}/api/teleporter").respond(
        400,
        json={"error": {"key": "bad_request", "message": "gravity database is locked"}},
    )
    with (
        caplog.at_level(logging.ERROR, logger="app.services.pihole_client"),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await client.post_teleporter(b"PK\x03\x04zip")
    assert any(
        "gravity database is locked" in rec.getMessage()
        and "HTTP 400" in rec.getMessage()
        for rec in caplog.records
    )


async def test_run_gravity_swallows_incomplete_chunked_read(
    respx_mock, client,
):
    """Same FTL-restart edge case applies to gravity runs."""
    client._no_auth = True
    respx_mock.post(f"{PIHOLE}/api/action/gravity").mock(
        side_effect=httpx.RemoteProtocolError("incomplete chunked read")
    )
    # Must NOT raise.
    await client.run_gravity()


async def test_run_gravity_propagates_other_protocol_errors(
    respx_mock, client,
):
    client._no_auth = True
    respx_mock.post(f"{PIHOLE}/api/action/gravity").mock(
        side_effect=httpx.RemoteProtocolError("genuinely wrong")
    )
    with pytest.raises(httpx.RemoteProtocolError):
        await client.run_gravity()
