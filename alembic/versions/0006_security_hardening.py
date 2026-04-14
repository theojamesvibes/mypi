"""Security hardening: password_change_required flag on users

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-14

Notes:
- api_password encryption (EncryptedString TypeDecorator) is handled at the
  ORM layer — no DDL change to pihole_instances.api_password is required.
  Existing plaintext rows are gracefully handled on read and re-synced from
  pihole_instances.yml by config_loader on startup.
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('password_change_required', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('users', 'password_change_required')
