# SPDX-License-Identifier: AGPL-3.0-only
"""Publications: the revision the target last accepted, and website-edit conflicts.

Adds the "conflict" status (the target was edited outside Georg and a reviewer must
decide), the conflicting fields and who edited them. Rows already published record their
revision as the last accepted one.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "status IN ('pending', 'publishing', 'published', 'failed', 'superseded')"
NEW = "status IN ('pending', 'publishing', 'published', 'failed', 'superseded', 'conflict')"


def upgrade() -> None:
    with op.batch_alter_table("publications", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_publications_status"), type_="check")
        batch_op.create_check_constraint("status", NEW)
        batch_op.add_column(sa.Column("published_revision_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("conflict_fields", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("conflict_by", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("conflict_at", sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_publications_published_revision_id_sermon_revisions"),
            "sermon_revisions",
            ["published_revision_id"],
            ["id"],
        )
    op.execute(
        "UPDATE publications SET published_revision_id = revision_id WHERE status = 'published'"
    )


def downgrade() -> None:
    op.execute("UPDATE publications SET status = 'failed' WHERE status = 'conflict'")
    with op.batch_alter_table("publications", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("fk_publications_published_revision_id_sermon_revisions"), type_="foreignkey"
        )
        batch_op.drop_column("conflict_at")
        batch_op.drop_column("conflict_by")
        batch_op.drop_column("conflict_fields")
        batch_op.drop_column("published_revision_id")
        batch_op.drop_constraint(op.f("ck_publications_status"), type_="check")
        batch_op.create_check_constraint("status", OLD)
