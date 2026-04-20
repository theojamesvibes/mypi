"""Add is_read_only scope to api_keys.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-19

Existing keys default to is_read_only=false, preserving current behaviour
on upgrade. New keys can be created as read-only from /api/auth/api-key
and will be rejected by mutation endpoints with HTTP 403.
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'api_keys',
        sa.Column('is_read_only', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('api_keys', 'is_read_only')
