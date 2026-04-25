"""Add vip_role to pihole_instances

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-25

Re-introduces a VIP-role flag on Pi-hole instances, distinct from the
old `is_hot_spare` boolean (migration 0011/0012). The 1.9.0 boolean was
flat — it suppressed Pushover alerts for an idle instance and nothing
more — and was ripped in 1.10.0 once the original "VIP standby flap" was
traced to an unrelated RPi3 FTL bug.

This column carries one of three values:
  - NULL      — instance is not part of any VIP cluster (current behavior)
  - "master"  — the configured-active node in its site's VIP cluster
  - "replica" — a standby in the same VIP cluster

The collector uses these to: (a) suppress per-instance stall alerts for
any instance with a non-NULL vip_role (idle is normal for the standby),
(b) fire a group-level stall alert only when *every* node in the cluster
goes flat at once, (c) fire a "VIP transfer" alert when traffic shifts
from one node to another inside the cluster.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pihole_instances",
        sa.Column("vip_role", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pihole_instances", "vip_role")
