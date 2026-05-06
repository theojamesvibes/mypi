"""Brute-force lockout: failed_login_count + failed_login_lockout_until on users

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-06

After Grok's v2.0.5 review flagged the absence of any built-in
brute-force protection beyond SlowAPI's per-IP rate limit (which
sidesteps trivially behind a NAT or any reverse proxy that aggregates
requests), this migration adds two columns on `users`:

  - failed_login_count       Counter for *consecutive* failed logins.
                             Reset to 0 on a successful login.
  - failed_login_lockout_until
                             When set in the future, all login attempts
                             for this user return 401 without checking
                             the password (and without leaking the
                             lockout state — same response shape as a
                             plain bad password).

Existing rows backfill with sensible defaults (0 / NULL).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "failed_login_lockout_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "failed_login_lockout_until")
    op.drop_column("users", "failed_login_count")
