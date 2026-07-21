"""Blocked-by-list attribution: query_logs.list_id + pihole_lists table

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-21

Adds the data behind the dashboard's "Blocked by list" breakdown:

  - query_logs.list_id   Pi-hole's id for the list that blocked a query
                         (adlist for gravity, else domainlist). NULL for
                         non-blocked queries and for all pre-existing rows —
                         there is no backfill; the breakdown fills forward as
                         new queries are polled.

  - pihole_lists         Per-instance mirror of GET /api/lists (block-type
                         adlists only), so a blocked query's list_id resolves
                         to a human name and an is_security flag (computed from
                         Pi-hole group membership at sync time).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("query_logs", sa.Column("list_id", sa.Integer(), nullable=True))

    op.create_table(
        "pihole_lists",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pihole_list_id", sa.Integer(), nullable=False),
        sa.Column("list_type", sa.String(length=16), nullable=False, server_default="block"),
        sa.Column("address", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("comment", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_security", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["instance_id"], ["pihole_instances.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instance_id", "pihole_list_id", "list_type",
            name="uq_piholelist_instance_listid_type",
        ),
    )
    op.create_index("ix_piholelist_instance", "pihole_lists", ["instance_id"])


def downgrade() -> None:
    op.drop_index("ix_piholelist_instance", table_name="pihole_lists")
    op.drop_table("pihole_lists")
    op.drop_column("query_logs", "list_id")
