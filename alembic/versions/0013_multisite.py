"""Multi-site scoping: sites, site_slug_history, site_settings; site_id on pihole_instances

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-24

Introduces per-site scoping. Existing single-site deployments are wrapped
into an auto-created `Default` site flagged as Main. Per-site app_settings
keys (`sync.*`, `pushover.*`, `poll.*`) move to the new `site_settings`
table under that Default site; truly-global keys stay in `app_settings`.

Pihole instance names lose their global UNIQUE and gain scoped
UNIQUE(site_id, name), so two sites can each have e.g. a "Living Room"
instance.

See docs/multisite-migration-plan.md for the full rationale.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create sites table.
    op.create_table(
        "sites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("is_main", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Partial unique indexes: one active name, one active slug, exactly one active Main.
    op.create_index(
        "ix_sites_active_name_unique", "sites", ["name"],
        unique=True, postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_sites_active_slug_unique", "sites", ["slug"],
        unique=True, postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_sites_one_active_main", "sites", ["is_main"],
        unique=True, postgresql_where=sa.text("is_main = true AND is_active = true"),
    )

    # 2. Slug history (permanent redirects on slug changes).
    op.create_table(
        "site_slug_history",
        sa.Column("old_slug", sa.String(64), primary_key=True),
        sa.Column("site_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )

    # 3. Per-site settings table (composite PK).
    op.create_table(
        "site_settings",
        sa.Column("site_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("site_id", "key"),
    )

    # 4. Add site_id to pihole_instances (nullable, will backfill then NOT NULL).
    op.add_column(
        "pihole_instances",
        sa.Column("site_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=True),
    )

    # 5. Create the Default site (Main).
    default_site_id = uuid.uuid4()
    op.execute(
        sa.text(
            "INSERT INTO sites (id, name, slug, is_main, is_active, sort_order) "
            "VALUES (:id, 'Default', 'default', TRUE, TRUE, 0)"
        ).bindparams(id=str(default_site_id))
    )

    # 6. Backfill: point every existing instance at Default.
    op.execute(
        sa.text("UPDATE pihole_instances SET site_id = :sid WHERE site_id IS NULL")
        .bindparams(sid=str(default_site_id))
    )

    # 7. Enforce NOT NULL now that every row has a site_id.
    op.alter_column("pihole_instances", "site_id", nullable=False)

    # 8. Swap the global name-uniqueness for scoped uniqueness.
    #    The original UNIQUE was inline in 0001 as `unique=True` on the name
    #    column → PostgreSQL auto-named it `pihole_instances_name_key`.
    op.drop_constraint("pihole_instances_name_key", "pihole_instances", type_="unique")
    op.create_unique_constraint(
        "uq_pihole_instances_site_name", "pihole_instances", ["site_id", "name"],
    )

    # 9. Move per-site app_settings keys into site_settings under Default.
    op.execute(
        sa.text(
            "INSERT INTO site_settings (site_id, key, value) "
            "SELECT :sid, key, value FROM app_settings "
            "WHERE key LIKE 'sync.%' "
            "   OR key LIKE 'pushover.%' "
            "   OR key LIKE 'poll.%'"
        ).bindparams(sid=str(default_site_id))
    )
    op.execute(
        sa.text(
            "DELETE FROM app_settings "
            "WHERE key LIKE 'sync.%' "
            "   OR key LIKE 'pushover.%' "
            "   OR key LIKE 'poll.%'"
        )
    )


def downgrade() -> None:
    # Flatten: move Main's site_settings back into app_settings; non-Main
    # site config is lost. Documented in the migration plan — downgrade is
    # a developer escape hatch, not routine.
    op.execute(
        sa.text(
            "INSERT INTO app_settings (key, value) "
            "SELECT ss.key, ss.value "
            "FROM site_settings ss "
            "JOIN sites s ON s.id = ss.site_id "
            "WHERE s.is_main = TRUE AND s.is_active = TRUE "
            "  AND ss.value IS NOT NULL "
            "ON CONFLICT (key) DO NOTHING"
        )
    )

    # Restore global name-uniqueness on pihole_instances.
    op.drop_constraint("uq_pihole_instances_site_name", "pihole_instances", type_="unique")
    op.create_unique_constraint(
        "pihole_instances_name_key", "pihole_instances", ["name"],
    )

    op.drop_column("pihole_instances", "site_id")

    op.drop_table("site_settings")
    op.drop_table("site_slug_history")

    op.drop_index("ix_sites_one_active_main", table_name="sites")
    op.drop_index("ix_sites_active_slug_unique", table_name="sites")
    op.drop_index("ix_sites_active_name_unique", table_name="sites")
    op.drop_table("sites")
