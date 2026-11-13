# SPDX-License-Identifier: AGPL-3.0-only
"""Viewer role, user email/last login/disabled, and the admin audit log.

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


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None, recreate="always") as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("disabled_at", sa.DateTime(), nullable=True))
        batch_op.drop_constraint(batch_op.f("ck_users_role"), type_="check")
        batch_op.create_check_constraint(
            batch_op.f("ck_users_role"), "role IN ('admin', 'reviewer', 'viewer')"
        )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_audit_log_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    # The old schema has no read-only role; dropping viewers never widens anyone's access.
    op.execute("DELETE FROM users WHERE role = 'viewer'")
    with op.batch_alter_table("users", schema=None, recreate="always") as batch_op:
        batch_op.drop_constraint(batch_op.f("ck_users_role"), type_="check")
        batch_op.create_check_constraint(
            batch_op.f("ck_users_role"), "role IN ('admin', 'reviewer')"
        )
        batch_op.drop_column("disabled_at")
        batch_op.drop_column("last_login_at")
        batch_op.drop_column("email")
