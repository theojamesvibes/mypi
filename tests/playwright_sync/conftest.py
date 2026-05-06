"""Fixtures for the master→replica sync E2E suite.

Two Pi-hole v6 emulators run on 127.0.0.1:9876 (master) and :9877
(replica); the MyPi app boots with a YAML that flags the first as
master. Tests trigger a sync via the /api/sync route and assert that
the replica received a /api/teleporter POST.

Required env vars (set by ui-tests workflow's `playwright-sync` job):
  DATABASE_URL              PostgreSQL DSN reachable from this process
                            *and* from the uvicorn subprocess.
  MYPI_SKIP_TESTCONTAINER   "1" — same reason as the other suites.
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
TEST_YAML = Path(__file__).parent / "pihole_instances.sync.yml"

MASTER_PORT = 9876
REPLICA_PORT = 9877
EMULATOR_PASSWORD = "emulator-password"

ADMIN_USERNAME = "admin"
ADMIN_BOOTSTRAP_PASSWORD = "sync-bootstrap-pw-321"
ADMIN_NEW_PASSWORD = "sync-rotated-pw-654"


def pytest_collection_modifyitems(config, items):
    rootpath = Path(__file__).parent.resolve()
    for item in items:
        try:
            item_path = Path(str(item.path)).resolve()
        except Exception:
            continue
        if rootpath in item_path.parents or item_path == rootpath:
            item.add_marker(pytest.mark.playwright)


# ── Helpers (mirror playwright_collector/conftest.py) ──────────────────────


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


def _spawn_emulator(name: str, port: int, log_basename: str) -> tuple[subprocess.Popen, "Any"]:
    env = os.environ.copy()
    env["EMULATOR_PIHOLE_PASSWORD"] = EMULATOR_PASSWORD
    env["EMULATOR_INSTANCE_NAME"] = name

    log_path = REPO_ROOT / log_basename
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "tests.emulators.pihole.app:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=REPO_ROOT, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
    )
    _wait_for(
        f"http://127.0.0.1:{port}/api/health",
        timeout=15.0, label=f"emulator '{name}' on :{port}",
    )
    return proc, log_fh


# ── Session: 2 emulators + live app ─────────────────────────────────────────


@pytest.fixture(scope="session")
def emulators() -> Iterator[dict[str, str]]:
    """Spawn the master + replica emulators and yield their URLs."""
    master_proc, master_log = _spawn_emulator(
        "sync-master", MASTER_PORT, "playwright-sync-master.log",
    )
    replica_proc, replica_log = _spawn_emulator(
        "sync-replica", REPLICA_PORT, "playwright-sync-replica.log",
    )
    try:
        yield {
            "master": f"http://127.0.0.1:{MASTER_PORT}",
            "replica": f"http://127.0.0.1:{REPLICA_PORT}",
        }
    finally:
        for proc in (master_proc, replica_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        master_log.close()
        replica_log.close()


@pytest.fixture(scope="session")
def live_app(emulators) -> Iterator[str]:
    if "DATABASE_URL" not in os.environ:
        pytest.skip("DATABASE_URL not set — skipping sync E2E suite.")

    env = os.environ.copy()
    env.setdefault("SECRET_KEY", "sync-test-secret-key-not-for-production")
    env["INITIAL_ADMIN_USER"] = ADMIN_USERNAME
    env["INITIAL_ADMIN_PASSWORD"] = ADMIN_BOOTSTRAP_PASSWORD
    env["PIHOLE_CONFIG_PATH"] = str(TEST_YAML)
    env["ENABLE_API_DOCS"] = "false"
    # Stats poll fast enough that both instances are visible in the UI
    # within the test's lifetime; queries poll on its DB-stored default
    # cadence (60s) — this suite doesn't assert on query rendering.
    env["STATS_POLL_INTERVAL"] = "5"
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

    log_path = REPO_ROOT / "playwright-sync-app.log"
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
        _wait_for(f"{base_url}/api/health", timeout=30.0, label="MyPi (sync mode)")
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


@pytest.fixture(scope="session")
def db_engine(live_app):
    from sqlalchemy import create_engine

    sync_url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql+psycopg://",
    )
    return create_engine(sync_url, future=True)


@pytest.fixture(scope="session")
def _auth_storage_state(live_app, browser, db_engine):
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
    context = browser.new_context(storage_state=_auth_storage_state)
    page = context.new_page()
    try:
        page.goto(f"{live_app}/")
        yield page
    finally:
        context.close()


@pytest.fixture(autouse=True)
def reset_emulators(emulators):
    """Reset both emulators' in-memory state before each test.

    Wipes the deny/allow lists *and* the sync_state counters so each
    test starts from a known zero baseline. (`__test__/reset` only
    clears deny/allow today; sync_state has no leak risk because the
    tests assert on deltas, not absolute counts — but a single reset
    keeps the contract simple.)
    """
    for url in (emulators["master"], emulators["replica"]):
        try:
            httpx.post(f"{url}/__test__/reset", timeout=2.0)
        except Exception:
            pass
