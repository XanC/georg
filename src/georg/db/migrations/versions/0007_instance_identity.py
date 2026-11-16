# SPDX-License-Identifier: AGPL-3.0-only
"""Instance identity: a permanent church UUID for publication keys.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15 22:00:00.000000
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instance_identity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("church_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_instance_identity_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instance_identity")),
        sa.UniqueConstraint("church_id", name=op.f("uq_instance_identity_church_id")),
    )
    identity = sa.table(
        "instance_identity",
        sa.column("id", sa.Integer),
        sa.column("church_id", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    op.execute(
        identity.insert().values(
            id=1, church_id=str(uuid.uuid4()), created_at=datetime.now(UTC).replace(tzinfo=None)
        )
    )


def downgrade() -> None:
    op.drop_table("instance_identity")
