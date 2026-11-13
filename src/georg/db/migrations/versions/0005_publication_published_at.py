# SPDX-License-Identifier: AGPL-3.0-only
"""Publications: when the target last accepted a write.

Georg sends this time to the WordPress bridge so a post written before georg-bridge 0.4.0
can be told apart from one saved on the website since. Rows already published take their
last update time, which is when they were marked published.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("published_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE publications SET published_at = updated_at WHERE status = 'published'")


def downgrade() -> None:
    with op.batch_alter_table("publications", schema=None) as batch_op:
        batch_op.drop_column("published_at")
