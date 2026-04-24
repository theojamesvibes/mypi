"""Move per-site settings rows from app_settings to site_settings (under Main).

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-24

Completes the data move that was intentionally deferred in migration 0013
(pre-release correction in dev.2 dropped the broken filter). This
migration moves four keys whose values have always been per-site in spirit
but lived in the flat `app_settings` table:

  - sync_schedule          (interval + auto_gravity + per-sync-run opts)
  - sync_last_result       (most recent sync outcome)
  - pushover_settings      (token, user_key, enabled, alert toggles)
  - queries_poll_interval  (stats/queries poll cadence)

All four get copied to `site_settings` under the currently-active Main
site. Fresh installs have no rows to move (app_settings is empty for
these keys) and the migration is a no-op.

Downgrade copies Main's values back to app_settings — lossy for any
non-Main overrides that get written after dev.5. Documented in the
migration plan.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEYS = (
    "sync_schedule",
    "sync_last_result",
    "pushover_settings",
    "queries_poll_interval",
)


def upgrade() -> None:
    # Copy each of the four keys from app_settings into site_settings under
    # the active Main site. If the destination row already exists (e.g.
    # someone hand-wrote one via direct SQL), preserve the existing value.
    keys_sql = ", ".join(f"'{k}'" for k in _KEYS)
    op.execute(sa.text(f"""
        INSERT INTO site_settings (site_id, key, value)
        SELECT m.id, a.key, a.value
        FROM app_settings AS a
        CROSS JOIN (
            SELECT id FROM sites
            WHERE is_main = TRUE AND is_active = TRUE
            LIMIT 1
        ) AS m
        WHERE a.key IN ({keys_sql})
        ON CONFLICT (site_id, key) DO NOTHING
    """))

    # Remove the now-migrated rows from app_settings so there's one source
    # of truth.
    op.execute(sa.text(f"""
        DELETE FROM app_settings
        WHERE key IN ({keys_sql})
    """))


def downgrade() -> None:
    keys_sql = ", ".join(f"'{k}'" for k in _KEYS)
    # Copy Main's values back to app_settings. Non-Main site overrides are
    # lost — documented in docs/multisite-migration-plan.md as a known
    # limitation of the downgrade path.
    op.execute(sa.text(f"""
        INSERT INTO app_settings (key, value)
        SELECT ss.key, ss.value
        FROM site_settings ss
        JOIN sites s ON s.id = ss.site_id
        WHERE s.is_main = TRUE AND s.is_active = TRUE
          AND ss.key IN ({keys_sql})
          AND ss.value IS NOT NULL
        ON CONFLICT (key) DO NOTHING
    """))
    op.execute(sa.text(f"""
        DELETE FROM site_settings
        WHERE key IN ({keys_sql})
    """))
