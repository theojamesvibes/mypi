"""Fixtures for the collector-end-to-end Playwright suite.

This suite differs from `tests/playwright/` in two ways:

  1. A Pi-hole v6 emulator (tests/emulators/pihole/app.py) runs as a
     subprocess on 127.0.0.1:9876 — *real* HTTP, *real* SID auth, *real*
     /api/queries pagination. The MyPi collector polls it on a short
     interval and writes the resulting StatsSnapshot + QueryLog rows.
  2. The MyPi app subprocess therefore boots with `STATS_POLL_INTERVAL=5`
     and `QUERIES_POLL_INTERVAL=2`, whereas the seeded-DB suite uses
     `999999` so the collector stays idle.

Tests assert on what flows from emulator → MyPi DB → dashboard, which
exercises the full polling pipeline (auth, SID persistence, summary
parsing, query backfill watermark) end-to-end. The seeded-DB suite
remains as the deterministic fast-feedback path.

Required env vars (set by the ui-tests workflow's `playwright-collector`
job; mirrors the seeded-DB suite):
  DATABASE_URL              PostgreSQL DSN reachable from this process
                            *and* from the uvicorn subprocess.
  MYPI_SKIP_TESTCONTAINER   Must be "1" so the parent tests/conftest.py
                            doesn't spin its own Postgres container.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_YAML = Path(__file__).parent / "pihole_instances.emulator.yml"

EMULATOR_HOST = "127.0.0.1"
EMULATOR_PORT = 9876
EMULATOR_PASSWORD = "emulator-password"

ADMIN_USERNAME = "admin"
ADMIN_BOOTSTRAP_PASSWORD = "collector-bootstrap-pw-789"
ADMIN_NEW_PASSWORD = "collector-rotated-pw-012"


def pytest_collection_modifyitems(config, items):
    """Auto-tag every test in this directory with the `playwright` marker.

    Mirrors the hook in tests/playwright/conftest.py — without it the
    default `pytest` run (configured `-m "not playwright"`) would still
    collect these and trip on alembic upgrade.
    """
    rootpath = Path(__file__).parent.resolve()
    for item in items:
        try:
            item_path = Path(str(item.path)).resolve()
        except Exception:
            continue
        if rootpath in item_path.parents or item_path == rootpath:
            item.add_marker(pytest.mark.playwright)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _wait_for(url: str, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return
        except Exception as exc:
            last_err = exc
        time.sleep(0.3)
    raise RuntimeError(
        f"{label} did not become healthy within {timeout}s "
        f"(last error: {last_err})"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Session: emulator + live app ────────────────────────────────────────────


@pytest.fixture(scope="session")
def pihole_emulator() -> Iterator[str]:
    """Spawn the Pi-hole v6 emulator on a fixed port.

    Fixed port (9876) so `pihole_instances.yml` can hardcode the URL —
    the MyPi config loader runs at app startup and we'd otherwise need
    to template the YAML at fixture time. Single-tenant CI runners
    make port collision a non-issue.
    """
    env = os.environ.copy()
    env["EMULATOR_PIHOLE_PASSWORD"] = EMULATOR_PASSWORD
    env["EMULATOR_INSTANCE_NAME"] = "emu-pihole-1"

    log_path = REPO_ROOT / "playwright-emulator.log"
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "tests.emulators.pihole.app:app",
            "--host", EMULATOR_HOST, "--port", str(EMULATOR_PORT),
            "--log-level", "warning",
        ],
        cwd=REPO_ROOT, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
    )

    try:
        _wait_for(
            f"http://{EMULATOR_HOST}:{EMULATOR_PORT}/api/health",
            timeout=15.0, label="Pi-hole emulator",
        )
        yield f"http://{EMULATOR_HOST}:{EMULATOR_PORT}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_fh.close()


@pytest.fixture(scope="session")
def live_app(pihole_emulator) -> Iterator[str]:
    """Start uvicorn in a subprocess pointed at the emulator YAML.

    Polling intervals are short (5 s stats / 2 s queries) so a typical
    test only waits a few seconds for the first poll to land in the DB.
    """
    if "DATABASE_URL" not in os.environ:
        pytest.skip("DATABASE_URL not set — skipping collector E2E suite.")

    env = os.environ.copy()
    env.setdefault("SECRET_KEY", "collector-test-secret-key-not-for-production")
    env["INITIAL_ADMIN_USER"] = ADMIN_USERNAME
    env["INITIAL_ADMIN_PASSWORD"] = ADMIN_BOOTSTRAP_PASSWORD
    env["PIHOLE_CONFIG_PATH"] = str(TEST_YAML)
    env["ENABLE_API_DOCS"] = "false"
    env["STATS_POLL_INTERVAL"] = "5"
    env["QUERIES_POLL_INTERVAL"] = "2"
    env.setdefault("DATA_RETENTION_DAYS", "999999")
    env["MYPI_SKIP_TESTCONTAINER"] = "1"

    sync_db_url = env["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql+psycopg://",
    )
    migrate_env = env.copy()
    migrate_env["DATABASE_URL"] = sync_db_url
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=migrate_env, check=True,
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    log_path = REPO_ROOT / "playwright-collector-app.log"
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "info",
        ],
        cwd=REPO_ROOT, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
    )

    try:
        _wait_for(f"{base_url}/api/health", timeout=30.0, label="MyPi (collector mode)")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_fh.close()


@pytest.fixture(scope="session")
def base_url(live_app) -> str:
    return live_app


# ── DB engine for read-only assertions + admin reset ────────────────────────


@pytest.fixture(scope="session")
def db_engine(live_app):
    from sqlalchemy import create_engine

    sync_url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql+psycopg://",
    )
    return create_engine(sync_url, future=True)


# ── Auth: cache storage state across tests ──────────────────────────────────


@pytest.fixture(scope="session")
def _auth_storage_state(live_app, browser, db_engine):
    """Log in once and capture cookies. Same rate-limit-avoidance pattern
    as the seeded-DB suite."""
    from sqlalchemy import text

    from app.auth import hash_password

    bootstrap_hash = hash_password(ADMIN_BOOTSTRAP_PASSWORD)
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET hashed_password = :h, "
                "password_change_required = TRUE WHERE username = :u"
            ),
            {"h": bootstrap_hash, "u": ADMIN_USERNAME},
        )

    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{live_app}/login")
        page.fill("#username", ADMIN_USERNAME)
        page.fill("#password", ADMIN_BOOTSTRAP_PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/change-password")

        page.fill("#current_password", ADMIN_BOOTSTRAP_PASSWORD)
        page.fill("#new_password", ADMIN_NEW_PASSWORD)
        page.fill("#confirm_password", ADMIN_NEW_PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url(f"{live_app}/")

        return context.storage_state()
    finally:
        context.close()


@pytest.fixture
def authed_page(live_app, browser, _auth_storage_state):
    """Authenticated page using the session-scoped storage state.

    This suite does *not* truncate stats/queries between tests because
    the collector is constantly writing to those tables — wiping them
    mid-suite would race with the next poll. Instead, tests poll-wait
    for stable values (e.g. "the dashboard stat card has updated to a
    non-dash value") and accept that data accumulates across tests.
    """
    context = browser.new_context(storage_state=_auth_storage_state)
    page = context.new_page()
    try:
        page.goto(f"{live_app}/")
        yield page
    finally:
        context.close()


@pytest.fixture(autouse=True)
def reset_emulator_state(pihole_emulator):
    """Wipe the emulator's deny/allow lists before each test.

    The emulator keeps its block/allow lists in process memory so it
    can answer GET requests after a block — without this reset, state
    leaks across tests and assertions like "deny list is empty" become
    order-dependent. Calls /__test__/reset (a test-only endpoint).
    """
    httpx.post(f"{pihole_emulator}/__test__/reset", timeout=2.0)


@pytest.fixture(scope="session")
def wait_for_first_poll(live_app, db_engine) -> bool:
    """Block until the collector has written ≥ 1 StatsSnapshot row.

    The first stats poll fires `STATS_POLL_INTERVAL` seconds after the
    job is added (5 s for this suite), plus the time it takes to auth
    against the emulator. Tests that read stats data depend on this
    fixture so they don't race the empty-DB window.
    """
    from sqlalchemy import text

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        with db_engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM stats_snapshots")).scalar()
        if count and count > 0:
            return True
        time.sleep(0.5)
    raise RuntimeError("Collector did not write any StatsSnapshot rows within 30 s")
