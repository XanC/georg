# SPDX-License-Identifier: AGPL-3.0-only
"""ORM models for the schema in docs/architecture.md §4.

Differences from §4, following §5.7: a sermon revision stores the four service
boundaries instead of a single range, and a service's source recording has ordered
parts (`source_parts`). Rules that need more than a column constraint (immutable
revisions, approving only the current revision) are SQLite triggers created by the
migrations and also checked by the repositories.
"""

from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from georg.db.types import UTCDateTime, utcnow

NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

USER_ROLES: Final = ("admin", "reviewer", "viewer")
EDITOR_ROLES: Final = ("admin", "reviewer")
"""Roles that may change editorial state; a viewer only reads."""
RUN_STATUSES: Final = ("queued", "running", "succeeded", "failed", "cancelled")
PUBLICATION_STATUSES: Final = (
    "pending",
    "publishing",
    "published",
    "failed",
    "superseded",
    "conflict",
)
ARTIFACT_KINDS: Final = (
    "source_media",
    "service_audio",
    "analysis_audio",
    "segments",
    "boundary_proposal",
    "sermon_audio",
    "transcript_raw",
    "transcript_formatted",
    "metadata_proposal",
    "rendition",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012
        datetime: UTCDateTime(),
        date: Date(),
        dict[str, Any]: JSON(),
        list[int]: JSON(),
        str: String(),
    }


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", USER_ROLES), name="role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    role: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    email: Mapped[str | None]
    last_login_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]


class AuditLog(Base):
    """Who changed what in the admin area (users, passwords, settings)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str]
    target: Mapped[str | None]
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("source_type", "source_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str]
    source_ref: Mapped[str]
    title: Mapped[str | None]
    service_date: Mapped[date | None]
    recorded_at: Mapped[datetime | None]
    url: Mapped[str | None]
    hints: Mapped[dict[str, Any]] = mapped_column("hints_json", default=dict)
    duration_ms: Mapped[int | None]
    discovered_at: Mapped[datetime] = mapped_column(default=utcnow)
    status_summary: Mapped[str | None]

    parts: Mapped[list["SourcePart"]] = relationship(
        back_populates="service", order_by="SourcePart.position", cascade="all, delete-orphan"
    )


class SourcePart(Base):
    """One media file of a service's recording, in timeline order (§5.7, D17)."""

    __tablename__ = "source_parts"
    __table_args__ = (
        UniqueConstraint("service_id", "position"),
        CheckConstraint("position >= 0", name="position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"))
    position: Mapped[int]
    ref: Mapped[str]
    name: Mapped[str]
    size: Mapped[int | None]
    checksum: Mapped[str | None]
    modified_at: Mapped[datetime | None]
    mime: Mapped[str | None]
    included: Mapped[bool] = mapped_column(default=True)
    """False when the reviewer excluded this part from the timeline."""

    service: Mapped[Service] = relationship(back_populates="parts")


class StageRun(Base):
    __tablename__ = "stage_runs"
    __table_args__ = (CheckConstraint(_in("status", RUN_STATUSES), name="status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str]
    module: Mapped[str]
    module_version: Mapped[str]
    params: Mapped[dict[str, Any]] = mapped_column("params_json", default=dict)
    input_artifact_ids: Mapped[list[int]] = mapped_column(default=list)
    cache_key: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(default="queued")
    attempt: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(_in("kind", ARTIFACT_KINDS), name="kind"),
        CheckConstraint("size >= 0", name="size"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str]
    storage_key: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int]
    mime: Mapped[str]
    duration_ms: Mapped[int | None]
    produced_by_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("stage_runs.id", ondelete="SET NULL")
    )
    meta: Mapped[dict[str, Any]] = mapped_column("meta_json", default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        CheckConstraint("max_attempts >= 1", name="max_attempts"),
        Index("ix_jobs_status_run_after", "status", "run_after"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column("payload_json", default=dict)
    status: Mapped[str] = mapped_column(default="queued")
    run_after: Mapped[datetime] = mapped_column(default=utcnow)
    lease_until: Mapped[datetime | None]
    worker_id: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=5)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    failure_notified_at: Mapped[datetime | None]
    """When reviewers were emailed about the job's permanent failure."""


class Sermon(Base):
    __tablename__ = "sermons"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    current_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("sermon_revisions.id", use_alter=True)
    )


class SermonRevision(Base):
    """An immutable snapshot of everything that would be published for a sermon.

    The only permitted update is filling `sermon_audio_artifact_id` once, because the
    sermon audio is derived from the revision's boundaries after the revision exists.
    """

    __tablename__ = "sermon_revisions"
    __table_args__ = (
        UniqueConstraint("sermon_id", "number"),
        CheckConstraint("author = 'system' OR author LIKE 'user:%'", name="author"),
        CheckConstraint(
            "0 <= service_start_ms AND service_start_ms <= sermon_start_ms"
            " AND sermon_start_ms < sermon_end_ms AND sermon_end_ms <= service_end_ms",
            name="boundaries",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sermon_id: Mapped[int] = mapped_column(ForeignKey("sermons.id", ondelete="CASCADE"))
    parent_revision_id: Mapped[int | None] = mapped_column(ForeignKey("sermon_revisions.id"))
    number: Mapped[int]
    author: Mapped[str]
    service_start_ms: Mapped[int]
    sermon_start_ms: Mapped[int]
    sermon_end_ms: Mapped[int]
    service_end_ms: Mapped[int]
    sermon_metadata: Mapped[dict[str, Any]] = mapped_column("metadata_json")
    transcript_markdown: Mapped[str | None] = mapped_column(Text)
    sermon_audio_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("artifacts.id"))
    field_origins: Mapped[dict[str, Any]] = mapped_column("field_origins_json", default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("sermon_revisions.id", ondelete="CASCADE"), index=True
    )
    approved_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime] = mapped_column(default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("sermon_id", "target_name"),
        CheckConstraint(_in("status", PUBLICATION_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sermon_id: Mapped[int] = mapped_column(ForeignKey("sermons.id", ondelete="CASCADE"))
    target_name: Mapped[str]
    revision_id: Mapped[int] = mapped_column(ForeignKey("sermon_revisions.id"))
    publication_key: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="pending")
    external_id: Mapped[str | None]
    external_url: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    published_revision_id: Mapped[int | None] = mapped_column(ForeignKey("sermon_revisions.id"))
    """The revision the target last accepted; `revision_id` is the one being published."""
    conflict_fields: Mapped[list[str] | None] = mapped_column(JSON)
    """Fields edited at the target that a "conflict" publication would overwrite."""
    conflict_by: Mapped[str | None]
    conflict_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]
    """When the target last accepted a write."""
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
