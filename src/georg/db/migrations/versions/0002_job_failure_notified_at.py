# SPDX-License-Identifier: AGPL-3.0-only
"""Record when reviewers were emailed about a failed job.

Jobs that had already failed are marked as notified so upgrading sends no backlog.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("failure_notified_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE jobs SET failure_notified_at = COALESCE(finished_at, created_at)"
        " WHERE status = 'failed'"
    )


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("failure_notified_at")
