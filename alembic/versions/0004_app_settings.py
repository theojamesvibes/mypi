"""app_settings key-value table

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-07
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
