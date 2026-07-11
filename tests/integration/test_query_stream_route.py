"""Route-level tests for the SSE live-query stream endpoints:
GET /api/queries/stream and GET /api/sites/{slug}/queries/stream.

Why these don't use ``client.stream(...)``: httpx 0.28's ASGITransport
runs the ASGI app to *completion* and buffers every body chunk before
returning the Response (see httpx._transports.asgi — ``await
self.app(scope, receive, send)`` followed by ``ASGIResponseStream``),
so an SSE response that never ends would hang the test forever.

Instead, ``_SSEConnection`` drives the app's ASGI callable directly
with a hand-rolled scope/receive/send triple. That still exercises the
real stack — routing, the BaseHTTPMiddleware layers, the auth
dependency, response headers — while letting the test read frames
incrementally and simulate a client disconnect (``http.disconnect``),
which is how Starlette's StreamingResponse learns to cancel the
generator. Every await is wrapped in ``asyncio.wait_for`` so a
regression turns into a test failure, never a hung suite.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

import app.api.queries as queries_api
from app.config import SESSION_COOKIE_NAME
from app.services import query_stream

OPEN_FRAME = b"event: open\ndata: \n\n"
TICK_FRAME = b"event: tick\ndata: \n\n"
KEEPALIVE_FRAME = b": keepalive\n\n"
ERROR_FRAME = b"event: error\ndata: subscriber limit reached\n\n"


@pytest.fixture(autouse=True)
def _clear_subscribers():
    """The subscriber set is process-global; start and end each test empty."""
    query_stream._subscribers.clear()
    yield
    query_stream._subscribers.clear()


@pytest.fixture
async def site(db_session):
    from app.models.site import Site

    s = Site(name="Main", slug="main", is_main=True, is_active=True, sort_order=0)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def session_cookie(authed_client):
    """`Cookie:` header value for an authenticated session, for requests
    made outside the httpx client (i.e. via _SSEConnection)."""
    token = authed_client.cookies.get(SESSION_COOKIE_NAME)
    assert token
    return f"{SESSION_COOKIE_NAME}={token}"


class _SSEConnection:
    """One in-process SSE request against the FastAPI app.

    ``async with`` starts the app as a task; exiting simulates a client
    disconnect and asserts the app shuts the response down promptly.
    """

    def __init__(self, app, path: str, cookie: str | None = None) -> None:
        self._app = app
        self._path = path
        self._cookie = cookie
        self._messages: asyncio.Queue[dict] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message) -> None:
        await self._messages.put(dict(message))

    async def __aenter__(self) -> _SSEConnection:
        headers = [
            (b"host", b"testserver"),
            (b"accept", b"text/event-stream"),
        ]
        if self._cookie:
            headers.append((b"cookie", self._cookie.encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "headers": headers,
            "scheme": "http",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 123),
            "root_path": "",
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._disconnect.set()
        assert self._task is not None
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            if exc_type is None:  # don't mask an in-flight test failure
                raise AssertionError(
                    "SSE response did not terminate within 5s of http.disconnect"
                ) from None

    async def start_message(self, timeout: float = 5.0) -> dict:
        msg = await asyncio.wait_for(self._messages.get(), timeout)
        assert msg["type"] == "http.response.start", msg
        return msg

    async def headers(self, timeout: float = 5.0) -> tuple[int, dict[str, str]]:
        msg = await self.start_message(timeout)
        return msg["status"], {k.decode().lower(): v.decode() for k, v in msg["headers"]}

    async def next_frame(self, timeout: float = 5.0) -> bytes:
        """Next non-empty body chunk. Raises on stream EOF."""
        while True:
            msg = await asyncio.wait_for(self._messages.get(), timeout)
            assert msg["type"] == "http.response.body", msg
            if msg.get("body"):
                return msg["body"]
            if not msg.get("more_body", False):
                raise AssertionError("SSE stream ended unexpectedly")

    async def wait_closed(self, timeout: float = 5.0) -> None:
        """Wait for the app to finish the response on its own (no disconnect)."""
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout)


# ── Auth guards ───────────────────────────────────────────────────────────────


async def test_stream_route_requires_auth(client):
    """Unauthenticated request never reaches the generator — plain 401.

    The wait_for guard means that if the auth dependency ever regressed
    to letting anonymous requests stream, this fails fast instead of
    hanging on ASGITransport's buffer-the-whole-body behaviour.
    """
    resp = await asyncio.wait_for(client.get("/api/queries/stream"), timeout=10.0)
    assert resp.status_code == 401


async def test_site_stream_route_requires_auth(client, site):
    resp = await asyncio.wait_for(
        client.get(f"/api/sites/{site.slug}/queries/stream"), timeout=10.0
    )
    assert resp.status_code == 401


async def test_site_stream_unknown_slug_is_404(authed_client):
    resp = await asyncio.wait_for(
        authed_client.get("/api/sites/no-such-site/queries/stream"), timeout=10.0
    )
    assert resp.status_code == 404


# ── Streaming behaviour ───────────────────────────────────────────────────────


async def test_stream_open_tick_headers_and_disconnect_cleanup(app, session_cookie):
    """Happy path on the cross-site stream: SSE headers, the initial
    `open` frame, a `tick` after publish, and — via __aexit__ — that
    client disconnect tears the response down and unsubscribes."""
    async with _SSEConnection(app, "/api/queries/stream", cookie=session_cookie) as conn:
        status, headers = await conn.headers()
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert "no-cache" in headers["cache-control"]
        assert "no-transform" in headers["cache-control"]
        assert headers["x-accel-buffering"] == "no"

        assert await conn.next_frame() == OPEN_FRAME
        # Subscription is registered before the open frame is yielded.
        assert query_stream.subscriber_count() == 1

        # A site_id=None subscriber sees publishes from any site.
        query_stream.publish(uuid.uuid4(), uuid.uuid4(), count=3)
        assert await conn.next_frame() == TICK_FRAME

    # Disconnect (context exit) must unregister the subscriber.
    assert query_stream.subscriber_count() == 0


async def test_site_stream_ticks_only_for_its_own_site(app, session_cookie, site):
    """The per-site stream must not leak ticks from other sites."""
    path = f"/api/sites/{site.slug}/queries/stream"
    async with _SSEConnection(app, path, cookie=session_cookie) as conn:
        status, headers = await conn.headers()
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert await conn.next_frame() == OPEN_FRAME

        # Publish for a *different* site: no frame may arrive.
        query_stream.publish(uuid.uuid4(), uuid.uuid4(), count=1)
        with pytest.raises(TimeoutError):
            await conn.next_frame(timeout=0.2)

        # Publish for the subscribed site: tick arrives.
        query_stream.publish(uuid.uuid4(), site.id, count=1)
        assert await conn.next_frame() == TICK_FRAME

    assert query_stream.subscriber_count() == 0


async def test_stream_sends_keepalive_comment_while_idle(app, session_cookie, monkeypatch):
    """With no publishes, the generator emits `: keepalive` comments on
    the heartbeat cadence (shrunk here so the test doesn't wait 25s)."""
    monkeypatch.setattr(queries_api, "_SSE_HEARTBEAT_SECONDS", 0.05)
    async with _SSEConnection(app, "/api/queries/stream", cookie=session_cookie) as conn:
        status, _ = await conn.headers()
        assert status == 200
        assert await conn.next_frame() == OPEN_FRAME
        assert await conn.next_frame(timeout=2.0) == KEEPALIVE_FRAME


async def test_stream_emits_error_frame_when_subscriber_cap_hit(
    app, session_cookie, monkeypatch
):
    """When the global subscriber cap is already in use, the route still
    answers 200 but sends one `error` event and ends the stream, so the
    client's fallback drops to polling instead of seeing a dead socket."""
    monkeypatch.setattr(query_stream, "_MAX_SUBSCRIBERS", 0)
    async with _SSEConnection(app, "/api/queries/stream", cookie=session_cookie) as conn:
        status, headers = await conn.headers()
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert await conn.next_frame() == ERROR_FRAME
        # The response must complete on its own — no disconnect needed.
        await conn.wait_closed()

    assert query_stream.subscriber_count() == 0
