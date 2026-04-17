"""Data migration: seed queries_poll_interval=10 for existing installs.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-17

New installs have no stats data yet when this migration runs, so the INSERT is
skipped and the app-code default of 60 s is used instead.  Existing installs
already polling at 10 s get 10 preserved so behavior is unchanged on upgrade.
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "INSERT INTO app_settings (key, value) "
        "SELECT 'queries_poll_interval', '{\"interval_seconds\": 10}' "
        "WHERE EXISTS (SELECT 1 FROM stats_snapshots LIMIT 1) "
        "ON CONFLICT (key) DO NOTHING"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM app_settings WHERE key = 'queries_poll_interval'"
    ))
