"""Add session_sid to pihole_instances

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-07

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pihole_instances",
        sa.Column("session_sid", sa.String(256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pihole_instances", "session_sid")
