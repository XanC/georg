# SPDX-License-Identifier: AGPL-3.0-only
"""Initial schema: tables from docs/architecture.md §4, plus integrity triggers.

Revision ID: 0001
Revises:
Create Date: 2026-11-01 20:45:06.309606
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

TRIGGERS = {
    "trg_sermon_revisions_immutable": """
        CREATE TRIGGER trg_sermon_revisions_immutable
        BEFORE UPDATE ON sermon_revisions
        WHEN OLD.sermon_audio_artifact_id IS NOT NULL
            OR NEW.id IS NOT OLD.id
            OR NEW.sermon_id IS NOT OLD.sermon_id
            OR NEW.parent_revision_id IS NOT OLD.parent_revision_id
            OR NEW.number IS NOT OLD.number
            OR NEW.author IS NOT OLD.author
            OR NEW.service_start_ms IS NOT OLD.service_start_ms
            OR NEW.sermon_start_ms IS NOT OLD.sermon_start_ms
            OR NEW.sermon_end_ms IS NOT OLD.sermon_end_ms
            OR NEW.service_end_ms IS NOT OLD.service_end_ms
            OR NEW.metadata_json IS NOT OLD.metadata_json
            OR NEW.transcript_markdown IS NOT OLD.transcript_markdown
            OR NEW.field_origins_json IS NOT OLD.field_origins_json
            OR NEW.content_hash IS NOT OLD.content_hash
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'sermon revisions are immutable');
        END
    """,
    "trg_sermons_current_revision_owner": """
        CREATE TRIGGER trg_sermons_current_revision_owner
        BEFORE UPDATE OF current_revision_id ON sermons
        WHEN NEW.current_revision_id IS NOT NULL
            AND (SELECT sermon_id FROM sermon_revisions WHERE id = NEW.current_revision_id)
                IS NOT NEW.id
        BEGIN
            SELECT RAISE(ABORT, 'current revision belongs to another sermon');
        END
    """,
    "trg_approvals_current_revision_only": """
        CREATE TRIGGER trg_approvals_current_revision_only
        BEFORE INSERT ON approvals
        WHEN NEW.revision_id IS NOT (
            SELECT s.current_revision_id
            FROM sermon_revisions r JOIN sermons s ON s.id = r.sermon_id
            WHERE r.id = NEW.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'only the current revision can be approved');
        END
    """,
    "trg_approvals_immutable": """
        CREATE TRIGGER trg_approvals_immutable
        BEFORE UPDATE ON approvals
        BEGIN
            SELECT RAISE(ABORT, 'approvals are immutable');
        END
    """,
}

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("run_after", sa.DateTime(), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("worker_id", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_jobs_status"),
        ),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_jobs_max_attempts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index("ix_jobs_status_run_after", ["status", "run_after"], unique=False)

    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("service_date", sa.Date(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("hints_json", sa.JSON(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("status_summary", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
        sa.UniqueConstraint(
            "source_type", "source_ref", name=op.f("uq_services_source_type_source_ref")
        ),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'reviewer')", name=op.f("ck_users_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "sermons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("current_revision_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["current_revision_id"],
            ["sermon_revisions.id"],
            name=op.f("fk_sermons_current_revision_id_sermon_revisions"),
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_sermons_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sermons")),
    )
    with op.batch_alter_table("sermons", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sermons_service_id"), ["service_id"], unique=False)

    op.create_table(
        "source_parts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(), nullable=True),
        sa.Column("modified_at", sa.DateTime(), nullable=True),
        sa.Column("mime", sa.String(), nullable=True),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.CheckConstraint("position >= 0", name=op.f("ck_source_parts_position")),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_source_parts_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_parts")),
        sa.UniqueConstraint(
            "service_id", "position", name=op.f("uq_source_parts_service_id_position")
        ),
    )
    op.create_table(
        "stage_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("module", sa.String(), nullable=False),
        sa.Column("module_version", sa.String(), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("input_artifact_ids", sa.JSON(), nullable=False),
        sa.Column("cache_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_stage_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_stage_runs_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stage_runs")),
    )
    with op.batch_alter_table("stage_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_stage_runs_cache_key"), ["cache_key"], unique=False)
        batch_op.create_index(batch_op.f("ix_stage_runs_service_id"), ["service_id"], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("produced_by_run_id", sa.Integer(), nullable=True),
        sa.Column("meta_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('source_media', 'service_audio', 'analysis_audio', 'segments',"
            " 'boundary_proposal', 'sermon_audio', 'transcript_raw', 'transcript_formatted',"
            " 'metadata_proposal', 'rendition')",
            name=op.f("ck_artifacts_kind"),
        ),
        sa.CheckConstraint("size >= 0", name=op.f("ck_artifacts_size")),
        sa.ForeignKeyConstraint(
            ["produced_by_run_id"],
            ["stage_runs.id"],
            name=op.f("fk_artifacts_produced_by_run_id_stage_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_artifacts_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_artifacts_service_id"), ["service_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_artifacts_sha256"), ["sha256"], unique=False)

    op.create_table(
        "sermon_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sermon_id", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.Integer(), nullable=True),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("service_start_ms", sa.Integer(), nullable=False),
        sa.Column("sermon_start_ms", sa.Integer(), nullable=False),
        sa.Column("sermon_end_ms", sa.Integer(), nullable=False),
        sa.Column("service_end_ms", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("transcript_markdown", sa.Text(), nullable=True),
        sa.Column("sermon_audio_artifact_id", sa.Integer(), nullable=True),
        sa.Column("field_origins_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "author = 'system' OR author LIKE 'user:%'", name=op.f("ck_sermon_revisions_author")
        ),
        sa.CheckConstraint(
            "0 <= service_start_ms AND service_start_ms <= sermon_start_ms"
            " AND sermon_start_ms < sermon_end_ms AND sermon_end_ms <= service_end_ms",
            name=op.f("ck_sermon_revisions_boundaries"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["sermon_revisions.id"],
            name=op.f("fk_sermon_revisions_parent_revision_id_sermon_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["sermon_audio_artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_sermon_revisions_sermon_audio_artifact_id_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["sermon_id"],
            ["sermons.id"],
            name=op.f("fk_sermon_revisions_sermon_id_sermons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sermon_revisions")),
        sa.UniqueConstraint(
            "sermon_id", "number", name=op.f("uq_sermon_revisions_sermon_id_number")
        ),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], name=op.f("fk_approvals_approved_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["sermon_revisions.id"],
            name=op.f("fk_approvals_revision_id_sermon_revisions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_approvals_revision_id"), ["revision_id"], unique=False)

    op.create_table(
        "publications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sermon_id", sa.Integer(), nullable=False),
        sa.Column("target_name", sa.String(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("publication_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("external_url", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed', 'superseded')",
            name=op.f("ck_publications_status"),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["sermon_revisions.id"],
            name=op.f("fk_publications_revision_id_sermon_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["sermon_id"],
            ["sermons.id"],
            name=op.f("fk_publications_sermon_id_sermons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publications")),
        sa.UniqueConstraint("publication_key", name=op.f("uq_publications_publication_key")),
        sa.UniqueConstraint(
            "sermon_id", "target_name", name=op.f("uq_publications_sermon_id_target_name")
        ),
    )
    for sql in TRIGGERS.values():
        op.execute(sql)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")

    op.drop_table("publications")
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_approvals_revision_id"))

    op.drop_table("approvals")
    op.drop_table("sermon_revisions")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifacts_sha256"))
        batch_op.drop_index(batch_op.f("ix_artifacts_service_id"))

    op.drop_table("artifacts")
    with op.batch_alter_table("stage_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_stage_runs_service_id"))
        batch_op.drop_index(batch_op.f("ix_stage_runs_cache_key"))

    op.drop_table("stage_runs")
    op.drop_table("source_parts")
    with op.batch_alter_table("sermons", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sermons_service_id"))

    op.drop_table("sermons")
    op.drop_table("users")
    op.drop_table("services")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_jobs_status_run_after")

    op.drop_table("jobs")
