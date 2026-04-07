"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-07

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(254), nullable=True, unique=True),
        sa.Column("hashed_password", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_username", "users", ["username"])

    # api_keys
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("key_hash", sa.String(256), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )

    # pihole_instances
    op.create_table(
        "pihole_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("api_password", sa.String(512), nullable=False, server_default=""),
        sa.Column("color", sa.String(16), nullable=False, server_default="#3c8dbc"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index("ix_pihole_instances_is_active", "pihole_instances", ["is_active"])

    # stats_snapshots
    op.create_table(
        "stats_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("pihole_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="online"),
        sa.Column("dns_queries_today", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("queries_blocked", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("percent_blocked", sa.Float(), nullable=False, server_default="0"),
        sa.Column("domains_on_blocklist", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("unique_clients", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queries_cached", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("queries_forwarded", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_stats_snapshots_collected_at", "stats_snapshots", ["collected_at"])
    op.create_index("ix_stats_instance_collected", "stats_snapshots", ["instance_id", "collected_at"])

    # query_logs
    op.create_table(
        "query_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("pihole_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pihole_query_id", sa.String(64), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("client_name", sa.String(253), nullable=True),
        sa.Column("query_type", sa.String(16), nullable=True),
        sa.Column("domain", sa.String(253), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("reply_type", sa.String(16), nullable=True),
        sa.Column("reply_time_ms", sa.Float(), nullable=True),
    )
    op.create_index("ix_querylog_instance_ts", "query_logs", ["instance_id", "timestamp"])
    op.create_index("ix_querylog_domain", "query_logs", ["domain"])
    op.create_index("ix_querylog_client", "query_logs", ["client_ip"])
    op.create_index("ix_querylog_pihole_id", "query_logs", ["instance_id", "pihole_query_id"])


def downgrade() -> None:
    op.drop_table("query_logs")
    op.drop_table("stats_snapshots")
    op.drop_index("ix_pihole_instances_is_active", table_name="pihole_instances")
    op.drop_table("pihole_instances")
    op.drop_table("api_keys")
    op.drop_table("users")
