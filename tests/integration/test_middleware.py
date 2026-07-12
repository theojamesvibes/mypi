"""Integration tests for HTTP middleware.

Covers the security-headers middleware (CSP / X-Frame-Options /
nosniff / Referrer-Policy on every response) and the wave-3 body-size
limit.
"""
from __future__ import annotations

# ── Security headers ─────────────────────────────────────────────────────────


async def test_health_response_has_security_headers(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "same-origin"
    csp = resp.headers.get("Content-Security-Policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # Neither script-src nor style-src may regress to 'unsafe-inline' —
    # page JS lives in /static/js with server data in JSON islands (issue
    # #84), and former style="" attributes live in dashboard.css classes
    # with dynamic colors applied via the CSSOM.
    directives = {
        d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()
    }
    assert directives["script-src"] == "script-src 'self' https://cdn.jsdelivr.net"
    assert directives["style-src"] == "style-src 'self' https://cdn.jsdelivr.net"


async def test_docs_pages_get_style_unsafe_inline_exception(client):
    """Swagger UI and ReDoc render with inline styles (upstream limitation),
    so /docs and /redoc — and only they — relax style-src. Scripts stay
    strict there too."""
    for path in ("/docs", "/redoc"):
        resp = await client.get(path)
        csp = resp.headers.get("Content-Security-Policy") or ""
        assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in csp, path
        assert "script-src 'self' https://cdn.jsdelivr.net" in csp, path


async def test_404_response_still_has_security_headers(client):
    """Error responses must carry the headers too — XSS in a 404
    page is just as exploitable."""
    resp = await client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Content-Security-Policy") is not None


async def test_hsts_only_present_when_secure_cookies_enabled(client, monkeypatch):
    """HSTS is opt-in via secure_cookies — without it, plain-HTTP
    deployments would silently break."""
    from app.config import settings

    monkeypatch.setattr(settings, "secure_cookies", False)
    resp = await client.get("/api/health")
    assert resp.headers.get("Strict-Transport-Security") is None

    monkeypatch.setattr(settings, "secure_cookies", True)
    resp2 = await client.get("/api/health")
    assert "max-age" in (resp2.headers.get("Strict-Transport-Security") or "")


# ── Body-size limit (wave-3) ─────────────────────────────────────────────────


async def test_oversized_body_returns_413(client):
    """1 MiB cap defended in middleware so a 5 MiB payload doesn't
    even hit the route handler."""
    big = "x" * (2 * 1024 * 1024)  # 2 MiB
    resp = await client.post(
        "/api/auth/login",
        content=big,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413


async def test_invalid_content_length_header_returns_400(client):
    resp = await client.post(
        "/api/auth/login",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": "not-a-number",
        },
    )
    # httpx may strip a malformed Content-Length before sending — in
    # that case the request just looks chunkless to the server and
    # returns the usual 422 from pydantic. Either rejection path is
    # acceptable; what matters is that the server doesn't crash.
    assert resp.status_code in (400, 422)


async def test_normal_body_passes_through(client):
    """Sanity: a small body still gets routed normally and processed
    by the login handler (which 401s a missing user)."""
    resp = await client.post(
        "/api/auth/login",
        json={"username": "noone", "password": "x"},
    )
    assert resp.status_code == 401  # auth fail, not 413
