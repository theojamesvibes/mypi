"""Drop is_hot_spare from pihole_instances

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-23

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("pihole_instances", "is_hot_spare")


def downgrade() -> None:
    op.add_column(
        "pihole_instances",
        sa.Column("is_hot_spare", sa.Boolean(), nullable=False, server_default="false"),
    )
