"""Add allowed_site_ids to api_keys (forward-compat hook for per-site scoping)

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-24

Nullable JSONB column on api_keys. NULL means the key has access to every
site on this MyPi deployment — the current v1 behavior and the only
behavior exposed to users today. A populated array will mean "scoped to
these site ids only," reserved for a future release that exposes the
scoping feature in the UI.

Adding the column now (instead of a later breaking migration) costs one
JSON column and one middleware null-check, and closes the door on a
future breaking schema change.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("allowed_site_ids", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "allowed_site_ids")
