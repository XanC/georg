# SPDX-License-Identifier: AGPL-3.0-only
"""Services: stage cache files used, and when retention cleanup removed the files.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("services", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cache_entries", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(sa.Column("purged_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("services", schema=None) as batch_op:
        batch_op.alter_column("cache_entries", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("services", schema=None) as batch_op:
        batch_op.drop_column("purged_at")
        batch_op.drop_column("cache_entries")
