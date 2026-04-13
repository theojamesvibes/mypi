"""Add version columns to pihole_instances

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('pihole_instances', sa.Column('version_core', sa.String(64), nullable=True))
    op.add_column('pihole_instances', sa.Column('version_ftl', sa.String(64), nullable=True))
    op.add_column('pihole_instances', sa.Column('version_web', sa.String(64), nullable=True))
    op.add_column('pihole_instances', sa.Column('update_available_core', sa.Boolean(), nullable=True))
    op.add_column('pihole_instances', sa.Column('update_available_ftl', sa.Boolean(), nullable=True))
    op.add_column('pihole_instances', sa.Column('update_available_web', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('pihole_instances', 'update_available_web')
    op.drop_column('pihole_instances', 'update_available_ftl')
    op.drop_column('pihole_instances', 'update_available_core')
    op.drop_column('pihole_instances', 'version_web')
    op.drop_column('pihole_instances', 'version_ftl')
    op.drop_column('pihole_instances', 'version_core')
