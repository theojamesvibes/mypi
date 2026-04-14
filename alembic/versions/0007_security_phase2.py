"""Security phase 2: JWT revocation table, API key HMAC upgrade column, verify_pihole_ssl

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-14

Notes:
- revoked_tokens: stores JTIs of logged-out JWTs until their natural expiry.
  Cleaned up nightly by cleanup_old_data().
- api_keys.key_hash_algo: tracks whether a stored key_hash is 'sha256' (legacy)
  or 'hmac-sha256' (1.4.0+).  Existing rows get server_default 'sha256' and are
  transparently rehashed on first verified use.
- No data migration required — column defaults handle all existing rows safely.
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'revoked_tokens',
        sa.Column('jti', sa.String(64), primary_key=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_revoked_tokens_expires_at', 'revoked_tokens', ['expires_at'])

    op.add_column(
        'api_keys',
        sa.Column('key_hash_algo', sa.String(16), nullable=False, server_default='sha256'),
    )


def downgrade() -> None:
    op.drop_column('api_keys', 'key_hash_algo')
    op.drop_index('ix_revoked_tokens_expires_at', table_name='revoked_tokens')
    op.drop_table('revoked_tokens')
