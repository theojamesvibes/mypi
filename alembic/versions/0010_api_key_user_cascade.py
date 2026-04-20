"""Add ON DELETE CASCADE to api_keys.user_id foreign key.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-19

The ORM-side relationship already declares cascade="all, delete-orphan",
so deleting a User from an open SQLAlchemy session will already cascade
to ApiKeys. The DB-side FK was created without ON DELETE CASCADE,
however, so a direct DB-level delete (psql, alembic data-fix script,
external tool) would fail with a FK violation. This brings the schema
in line with the ORM intent.
"""
from alembic import op


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


_FK_NAME = 'api_keys_user_id_fkey'


def upgrade() -> None:
    op.drop_constraint(_FK_NAME, 'api_keys', type_='foreignkey')
    op.create_foreign_key(
        _FK_NAME,
        'api_keys', 'users',
        ['user_id'], ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, 'api_keys', type_='foreignkey')
    op.create_foreign_key(
        _FK_NAME,
        'api_keys', 'users',
        ['user_id'], ['id'],
    )
