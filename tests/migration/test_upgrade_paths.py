"""Upgrade-path tests: simulate an old MyPi install and run alembic forward.

The goal is to catch the failure mode where a migration silently drops
or corrupts data on a real upgrade. For each anchor revision below we:

  1. Spin a fresh Postgres testcontainer.
  2. Run ``alembic upgrade <anchor>``.
  3. Seed a small set of rows via raw SQL (using the column set that
     existed *at* that revision — so we don't accidentally rely on
     columns that don't exist yet).
  4. Run ``alembic upgrade head``.
  5. Assert the rows survived and any new required columns have sane
     defaults. Where the migration is a data-migration (e.g. 0013
     introduces sites and backfills site_id), assert the post-migration
     relationships are correct.

Anchor revisions chosen for coverage of the most invasive transitions:

  * ``0006`` — early v1 schema (initial + first round of security
    hardening). Tests the long-arc upgrade across every subsequent
    migration.
  * ``0010`` — mid-v1 schema (API key shape stabilised after the
    SHA-256 → HMAC rotation hooks). Catches regressions specific to
    the migrations introduced after API-key changes.
  * ``0013`` — v2.0 multi-site introduction. The migration that
    creates ``sites``, adds ``site_id`` to ``pihole_instances``, and
    backfills via raw SQL — easily the riskiest single migration in
    the chain.

Each test spins its own container so a destructive bug can't leak
between tests. testcontainers cleans up automatically when the ``with``
block exits.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer


REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic(db_url: str, target: str) -> None:
    """Run ``alembic upgrade <target>`` against ``db_url``.

    Subprocess invocation so the migration runs in its own interpreter
    with its own settings — no risk of alembic env.py picking up state
    cached in the test process. The URL is normalised to the asyncpg
    driver because env.py uses ``async_engine_from_config``; passing a
    psycopg URL trips ``InvalidRequestError`` on engine construction.
    """
    async_url = db_url.replace("postgresql+psycopg://", "postgresql+asyncpg://")

    env = os.environ.copy()
    env["DATABASE_URL"] = async_url
    # SECRET_KEY is required by app.config; alembic's env.py imports it
    # transitively. Any non-empty value works.
    env.setdefault("SECRET_KEY", "migration-test-secret-not-for-production")
    env["MYPI_SKIP_TESTCONTAINER"] = "1"
    subprocess.run(
        ["alembic", "upgrade", target],
        cwd=REPO_ROOT, env=env, check=True,
    )


def _make_engine(pg: PostgresContainer):
    """Return a sync SQLAlchemy engine for the testcontainer.

    psycopg v3 driver — same as the migration step, so no driver-skew
    surprises (asyncpg's behaviour on UUID binding differs from psycopg).
    """
    return create_engine(pg.get_connection_url(), future=True)


# ── Anchor: revision 0006 (early v1 schema) ─────────────────────────────────


def test_upgrade_from_0006_preserves_user_and_instance():
    """An early-v1 install with one user + one Pi-hole instance must
    survive every subsequent migration intact, including the multi-site
    backfill at 0013.

    At rev 0006 the ``pihole_instances`` table has *no* site_id column
    and instance names are globally unique. After upgrading to head we
    expect:
      - The user row still exists with its hashed_password preserved
      - The instance row exists, points at a non-null site_id, and the
        site it points to is the auto-created Main ("Default")
    """
    with PostgresContainer("postgres:18-alpine", driver="psycopg") as pg:
        sync_url = pg.get_connection_url()

        # Migrate to anchor.
        _alembic(sync_url, "0006")

        engine = _make_engine(pg)
        user_id = uuid.uuid4()
        instance_id = uuid.uuid4()
        with engine.begin() as conn:
            # Schema at 0006: users.password_change_required exists from 0006.
            conn.execute(
                text(
                    "INSERT INTO users (id, username, hashed_password, "
                    " is_active, password_change_required) "
                    "VALUES (CAST(:id AS uuid), :u, :h, TRUE, FALSE)"
                ),
                {
                    "id": str(user_id), "u": "anchor_user_v1",
                    "h": "$2b$12$abcdefghijklmnopqrstuv",
                },
            )
            # Pre-multisite instance: no site_id column yet.
            conn.execute(
                text(
                    "INSERT INTO pihole_instances "
                    "(id, name, url, api_password, color, is_active) "
                    "VALUES (CAST(:id AS uuid), :name, :url, '', '#3c8dbc', TRUE)"
                ),
                {
                    "id": str(instance_id),
                    "name": "anchor-pi", "url": "http://10.0.0.1",
                },
            )

        # Now run the rest of the chain.
        _alembic(sync_url, "head")

        with engine.connect() as conn:
            # User survived.
            row = conn.execute(
                text("SELECT username, hashed_password FROM users WHERE id = CAST(:id AS uuid)"),
                {"id": str(user_id)},
            ).first()
            assert row is not None, "User row was lost across migrations"
            assert row.username == "anchor_user_v1"
            assert row.hashed_password.startswith("$2b$")

            # Instance survived and was backfilled into Default site.
            row = conn.execute(
                text(
                    "SELECT pi.name, pi.url, pi.site_id, s.name AS site_name, "
                    "       s.is_main, s.slug "
                    "FROM pihole_instances pi "
                    "JOIN sites s ON pi.site_id = s.id "
                    "WHERE pi.id = CAST(:id AS uuid)"
                ),
                {"id": str(instance_id)},
            ).first()
            assert row is not None, "Instance row was lost across migrations"
            assert row.name == "anchor-pi"
            assert row.url == "http://10.0.0.1"
            assert row.site_id is not None
            # The 0013 migration creates exactly one site named "Default"
            # and flips it to is_main=True.
            assert row.site_name == "Default"
            assert row.is_main is True
            assert row.slug == "default"


# ── Anchor: revision 0010 (mid-v1 schema) ───────────────────────────────────


def test_upgrade_from_0010_api_key_survives():
    """An install at rev 0010 has API keys with the legacy SHA-256
    hash format and the user-cascade FK in place. Verify that an API
    key seeded at this point still resolves to its user after every
    later migration runs (in particular 0014, which adds
    allowed_site_ids).
    """
    with PostgresContainer("postgres:18-alpine", driver="psycopg") as pg:
        sync_url = pg.get_connection_url()
        _alembic(sync_url, "0010")

        engine = _make_engine(pg)
        user_id = uuid.uuid4()
        api_key_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, hashed_password, "
                    " is_active, password_change_required) "
                    "VALUES (CAST(:id AS uuid), :u, :h, TRUE, FALSE)"
                ),
                {
                    "id": str(user_id), "u": "anchor_user_v18",
                    "h": "$2b$12$abcdefghijklmnopqrstuv",
                },
            )
            # API key at rev 0010: is_read_only column added at 0009;
            # legacy_hash_format column added at 0007.
            conn.execute(
                text(
                    "INSERT INTO api_keys "
                    "(id, user_id, name, key_hash, is_active, is_read_only) "
                    "VALUES (CAST(:id AS uuid), CAST(:uid AS uuid), :n, :h, TRUE, FALSE)"
                ),
                {
                    "id": str(api_key_id), "uid": str(user_id),
                    "n": "ios-app", "h": "x" * 64,
                },
            )

        _alembic(sync_url, "head")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT ak.name, ak.is_active, ak.is_read_only, u.username "
                    "FROM api_keys ak JOIN users u ON ak.user_id = u.id "
                    "WHERE ak.id = CAST(:id AS uuid)"
                ),
                {"id": str(api_key_id)},
            ).first()
            assert row is not None, "API key row was lost across migrations"
            assert row.name == "ios-app"
            assert row.is_active is True
            assert row.is_read_only is False
            assert row.username == "anchor_user_v18"


# ── Anchor: revision 0013 (multi-site introduction) ────────────────────────


def test_upgrade_from_0013_preserves_site_and_instance():
    """An install at 0013 already has the Default site backfilled.
    Verify that subsequent migrations (0014 api_key.allowed_site_ids,
    0015 settings move, 0016 vip_role) leave the site and its
    instances intact.
    """
    with PostgresContainer("postgres:18-alpine", driver="psycopg") as pg:
        sync_url = pg.get_connection_url()
        _alembic(sync_url, "0013")

        engine = _make_engine(pg)
        site_id = uuid.uuid4()
        instance_id = uuid.uuid4()
        with engine.begin() as conn:
            # 0013 already created a "Default" site and stored it as
            # the Main; we add a second site with our own data so we
            # can assert that the row carrying our specific name
            # survives.
            conn.execute(
                text(
                    "INSERT INTO sites (id, name, slug, is_main, is_active, sort_order) "
                    "VALUES (CAST(:id AS uuid), :n, :slug, FALSE, TRUE, 1)"
                ),
                {"id": str(site_id), "n": "Anchor Site", "slug": "anchor-site"},
            )
            conn.execute(
                text(
                    "INSERT INTO pihole_instances "
                    "(id, site_id, name, url, api_password, color, is_active, is_master) "
                    "VALUES (CAST(:id AS uuid), CAST(:sid AS uuid), :n, :u, "
                    "        '', '#00a65a', TRUE, TRUE)"
                ),
                {
                    "id": str(instance_id), "sid": str(site_id),
                    "n": "anchor-replica", "u": "http://10.0.0.2",
                },
            )

        _alembic(sync_url, "head")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT s.name AS site_name, s.is_main, pi.name AS pi_name, "
                    "       pi.is_master, pi.vip_role "
                    "FROM sites s JOIN pihole_instances pi ON pi.site_id = s.id "
                    "WHERE s.id = CAST(:id AS uuid)"
                ),
                {"id": str(site_id)},
            ).first()
            assert row is not None, "Site + instance pair was lost"
            assert row.site_name == "Anchor Site"
            assert row.is_main is False  # we explicitly set this site as non-Main
            assert row.pi_name == "anchor-replica"
            assert row.is_master is True
            # 0016 added vip_role as nullable — pre-existing rows should
            # come out the other side with NULL, not blow up the migration.
            assert row.vip_role is None


# ── Anchor: revision 0014 (pre-settings-move data migration) ───────────────


def test_upgrade_from_0014_moves_app_settings_to_site_settings():
    """0015 is a *data* migration: it copies four keys from
    `app_settings` (flat, global) into `site_settings` (per-site,
    under Main) and then deletes the originals. This is the migration
    most likely to silently corrupt data — a row stuck in app_settings
    won't break startup but will leave the operator's persisted
    sync schedule / Pushover creds / poll interval invisible.

    Verifies:
      - Each of the four moved keys lands in site_settings under the
        Main site row 0013 created (slug='default', is_main=TRUE).
      - The corresponding rows are gone from app_settings.
      - `encryption_key` (a row 0015 should *not* touch) survives in
        app_settings — guards against the SQL `IN (…)` accidentally
        being too wide.
    """
    with PostgresContainer("postgres:18-alpine", driver="psycopg") as pg:
        sync_url = pg.get_connection_url()
        _alembic(sync_url, "0014")

        engine = _make_engine(pg)

        # Seed app_settings with the four keys 0015 moves, plus one
        # key it must leave alone.
        moved_keys = {
            "sync_schedule": '{"interval_minutes": 30, "auto_gravity": true}',
            "sync_last_result": '{"status": "ok", "ts": 1700000000}',
            "pushover_settings": '{"enabled": true, "token": "tok", "user_key": "usr"}',
            "queries_poll_interval": '{"interval_seconds": 15}',
        }
        unrelated_key = ("encryption_key", "fake-fernet-key-base64")

        with engine.begin() as conn:
            for key, value in moved_keys.items():
                conn.execute(
                    text(
                        "INSERT INTO app_settings (key, value) "
                        "VALUES (:k, :v)"
                    ),
                    {"k": key, "v": value},
                )
            conn.execute(
                text(
                    "INSERT INTO app_settings (key, value) "
                    "VALUES (:k, :v)"
                ),
                {"k": unrelated_key[0], "v": unrelated_key[1]},
            )

        _alembic(sync_url, "head")

        with engine.connect() as conn:
            # 1. site_settings now carries each moved key, under Main.
            for key, expected_value in moved_keys.items():
                row = conn.execute(
                    text(
                        "SELECT ss.value FROM site_settings ss "
                        "JOIN sites s ON s.id = ss.site_id "
                        "WHERE s.is_main = TRUE AND s.is_active = TRUE "
                        "  AND ss.key = :k"
                    ),
                    {"k": key},
                ).first()
                assert row is not None, (
                    f"site_settings missing key '{key}' after 0015"
                )
                assert row.value == expected_value, (
                    f"site_settings value for '{key}' was rewritten: "
                    f"got {row.value!r}, expected {expected_value!r}"
                )

            # 2. app_settings no longer has the moved keys.
            for key in moved_keys:
                row = conn.execute(
                    text("SELECT 1 FROM app_settings WHERE key = :k"),
                    {"k": key},
                ).first()
                assert row is None, (
                    f"app_settings still has key '{key}' after 0015 "
                    "— the DELETE step did not run"
                )

            # 3. unrelated_key (encryption_key) is *still* in app_settings.
            row = conn.execute(
                text("SELECT value FROM app_settings WHERE key = :k"),
                {"k": unrelated_key[0]},
            ).first()
            assert row is not None, (
                "encryption_key was wiped from app_settings — 0015's "
                "key filter is too wide"
            )
            assert row.value == unrelated_key[1]


# ── Sanity: clean install upgrade-to-head works ─────────────────────────────


def test_clean_install_upgrade_to_head():
    """A fresh DB upgraded straight to head must succeed.

    This is essentially what the existing testcontainer does in
    tests/conftest.py via Base.metadata.create_all, but here we
    actually run alembic top-to-bottom — catching the case where a
    migration is internally consistent but the chain as a whole
    has a missing dependency.
    """
    with PostgresContainer("postgres:18-alpine", driver="psycopg") as pg:
        sync_url = pg.get_connection_url()
        _alembic(sync_url, "head")

        engine = _make_engine(pg)
        with engine.connect() as conn:
            # Spot-check that the major tables landed.
            for table in [
                "users", "api_keys", "pihole_instances",
                "stats_snapshots", "query_logs", "sites",
                "site_settings", "site_slug_history", "app_settings",
            ]:
                exists = conn.execute(
                    text(
                        "SELECT EXISTS ("
                        " SELECT FROM information_schema.tables "
                        " WHERE table_schema = 'public' AND table_name = :t"
                        ")"
                    ),
                    {"t": table},
                ).scalar()
                assert exists, f"Expected table '{table}' missing after upgrade head"
