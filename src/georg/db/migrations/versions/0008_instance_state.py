# SPDX-License-Identifier: AGPL-3.0-only
"""Instance state: active, paused (imported, awaiting `georg resume`), or frozen
(after `georg export`, permanently read-only for discovery and publishing).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot ALTER a table's constraints; batch mode rebuilds the table.
    with op.batch_alter_table("instance_identity") as batch:
        batch.add_column(sa.Column("state", sa.String(), nullable=False, server_default="active"))
        batch.create_check_constraint(
            op.f("ck_instance_identity_state"),
            "state IN ('active', 'paused', 'frozen')",
        )


def downgrade() -> None:
    with op.batch_alter_table("instance_identity") as batch:
        batch.drop_constraint(op.f("ck_instance_identity_state"), type_="check")
        batch.drop_column("state")
