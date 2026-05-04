"""Test-suite-wide setup.

Runs *before* any `from app.*` import so that pydantic-settings can
construct `Settings()` without raising on missing required env vars.
Unit tests don't touch the DB, so the DATABASE_URL value just needs to
parse — nothing connects to it.
"""
from __future__ import annotations

import os

# Required by app.config.Settings — must be set before any app import.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5432/mypi_test",
)
os.environ.setdefault("SECRET_KEY", "unit-test-secret-key-not-for-production")
