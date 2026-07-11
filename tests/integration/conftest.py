"""Fixtures shared by the integration suite.

The test Postgres + schema are already up by the time these fixtures
run (see tests/conftest.py module-load setup). All we do here is
provide per-test isolation (truncate-all between tests), an HTTP
client wired through the FastAPI ASGI transport, and a small set of
factory fixtures that create users/keys for the tests to authenticate
as.

Session-scoped event loop is configured globally in pytest.ini —
required so the SQLAlchemy async engine's connection pool can reuse
asyncpg connections across tests.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text

# ── DB isolation ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    """Wipe every table before each test so state can't leak between them.

    `alembic_version` is preserved so the DB stays "migrated" across
    truncations. Schema is set up once at session start by `create_all`.
    """
    from app.database import engine

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename != 'alembic_version'"
            )
        )
        tables = [row[0] for row in result.fetchall()]
        if tables:
            await conn.execute(
                text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE")
            )
    yield


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limiter():
    """slowapi's Limiter is process-global — without resetting between
    tests the rate-limit-fires test would poison every following test
    that hits the same path."""
    from app.limiter import limiter

    limiter.reset()
    yield


# ── App + HTTP client ────────────────────────────────────────────────────────


@pytest.fixture
def app():
    """Import the FastAPI app once per test. Lifespan startup hooks
    (admin bootstrap, encryption-key resolution, scheduler) are NOT
    invoked — tests set up exactly the state they need.
    """
    from app.main import app as _app
    return _app


@pytest_asyncio.fixture
async def client(app):
    """Plain HTTPX client wired through the in-process ASGI transport.

    No cookies, no auth — tests that need to authenticate either log in
    or set the `X-API-Key` header explicitly. See `authed_client`.
    """
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


# ── Factories ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session():
    """A direct SQLAlchemy session for tests that need to read/write
    DB state outside the API surface (factories, assertions on
    persistence)."""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def test_user(db_session):
    """Create one user per test with a known plaintext password.

    Returns ``(user, raw_password)`` so tests can both reference the
    user object and post the cleartext password to /api/auth/login.
    """
    from app.auth import hash_password
    from app.models.user import User

    raw_password = "test-password-123"
    user = User(
        username="alice",
        hashed_password=hash_password(raw_password),
        is_active=True,
        password_change_required=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user, raw_password


@pytest_asyncio.fixture
async def authed_client(client, test_user):
    """Client with a valid session cookie. Use for any web/API test
    that needs an authenticated *human* (not an API key)."""
    user, password = test_user
    resp = await client.post(
        "/api/auth/login",
        json={"username": user.username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return client


@pytest_asyncio.fixture
async def api_key(authed_client):
    """Mint a fresh read-write API key under the test user. Returns
    the raw key so tests can put it in the X-API-Key header."""
    resp = await authed_client.post(
        "/api/auth/api-key",
        json={"name": "test-key", "is_read_only": False},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["raw_key"]


@pytest_asyncio.fixture
async def readonly_api_key(authed_client):
    """Same as `api_key` but read-only — used to test that mutations
    are blocked with 403."""
    resp = await authed_client.post(
        "/api/auth/api-key",
        json={"name": "ro-key", "is_read_only": True},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["raw_key"]
