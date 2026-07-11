"""Test-suite-wide setup.

Runs *before* any `from app.*` import so that pydantic-settings can
construct `Settings()` without raising on missing required env vars,
and so the engine in `app.database` points at the test Postgres rather
than a non-existent one.

By default we spin up a real Postgres testcontainer and create the
schema with `Base.metadata.create_all`. Set `MYPI_SKIP_TESTCONTAINER=1`
to skip the container — useful for IDE runs that target only the unit
suite (which never actually opens a DB connection).
"""
from __future__ import annotations

import atexit
import os

# ── Required env for app.config.Settings() ───────────────────────────────────
os.environ.setdefault("SECRET_KEY", "unit-test-secret-key-not-for-production")

if os.environ.get("MYPI_SKIP_TESTCONTAINER") == "1":
    # Unit tests don't need a real DB; a parsable DSN is enough so
    # pydantic-settings's validate_database_url passes.
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://test:test@localhost:5432/mypi_test",
    )
else:
    from testcontainers.postgres import PostgresContainer

    # postgres:18-alpine matches the prod compose stack. We use psycopg
    # v3 for the sync / migration path because v2 has no prebuilt wheel
    # for cp313+. Runtime stays on asyncpg; the two drivers don't share
    # a connection so this never affects prod.
    _pg = PostgresContainer("postgres:18-alpine", driver="psycopg")
    _pg.start()
    atexit.register(_pg.stop)

    sync_url = _pg.get_connection_url()
    async_url = sync_url.replace("postgresql+psycopg://", "postgresql+asyncpg://")
    os.environ["DATABASE_URL"] = async_url

    # Now safe to import app.* — settings picks up the testcontainer DSN
    # and app.database.engine is created against it. We bring up the
    # schema synchronously via psycopg2 so all integration fixtures find
    # tables already in place.
    from sqlalchemy import create_engine

    import app.models.pihole  # noqa: F401
    import app.models.settings  # noqa: F401
    import app.models.site  # noqa: F401

    # Importing the model modules registers their tables on Base.metadata.
    import app.models.user  # noqa: F401
    from app.database import Base

    _sync_engine = create_engine(sync_url)
    Base.metadata.create_all(_sync_engine)
    _sync_engine.dispose()
