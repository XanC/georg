# SPDX-License-Identifier: AGPL-3.0-only
"""Repositories: the only code that reads and writes the editorial tables.

Each repository wraps a caller-owned `Session`; the caller decides the transaction
boundaries (use `georg.db.session_factory`, whose sessions begin `IMMEDIATE`).
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from georg.core.errors import GeorgError
from georg.core.types import (
    ArtifactRef,
    ExternalRef,
    SermonMetadata,
    ServiceBoundaries,
    SourceItem,
)
from georg.core.types import (
    SourcePart as SourcePartValue,
)
from georg.db.models import (
    ARTIFACT_KINDS,
    USER_ROLES,
    Approval,
    Artifact,
    Publication,
    Sermon,
    SermonRevision,
    Service,
    SourcePart,
    User,
)
from georg.db.types import utcnow


class RepositoryError(GeorgError):
    """A repository operation would break an invariant of the data model."""


class NotFoundError(RepositoryError, LookupError):
    pass


class ImmutableRevisionError(RepositoryError):
    pass


class NotCurrentRevisionError(RepositoryError):
    pass


def content_hash(
    boundaries: ServiceBoundaries, metadata: SermonMetadata, transcript_markdown: str | None
) -> str:
    """Hash of a revision's editorial content: boundaries, metadata, and transcript."""
    doc = {
        "boundaries": boundaries.model_dump(mode="json"),
        "metadata": metadata.model_dump(mode="json"),
        "transcript_markdown": transcript_markdown,
    }
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _get[T](session: Session, model: type[T], ident: int) -> T:
    obj = session.get(model, ident)
    if obj is None:
        raise NotFoundError(f"{model.__name__} {ident} not found")
    return obj


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, username: str, password_hash: str, role: str) -> User:
        if role not in USER_ROLES:
            raise RepositoryError(f"unknown role {role!r}")
        user = User(username=username, password_hash=password_hash, role=role)
        self.session.add(user)
        self.session.flush()
        return user

    def get(self, user_id: int) -> User:
        return _get(self.session, User, user_id)

    def by_username(self, username: str) -> User | None:
        return self.session.scalars(select(User).where(User.username == username)).one_or_none()


class ServiceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, service_id: int) -> Service:
        return _get(self.session, Service, service_id)

    def newest_first(self, limit: int | None = None) -> Sequence[Service]:
        """Services by service date (undated last), then by discovery, newest first."""
        stmt = select(Service).order_by(
            Service.service_date.desc().nulls_last(),
            Service.discovered_at.desc(),
            Service.id.desc(),
        )
        return self.session.scalars(stmt.limit(limit)).all()

    def by_source(self, source_type: str, source_ref: str) -> Service | None:
        stmt = select(Service).where(
            Service.source_type == source_type, Service.source_ref == source_ref
        )
        return self.session.scalars(stmt).one_or_none()

    def add_discovered(self, item: SourceItem) -> tuple[Service, bool]:
        """Record a discovered recording once; return the service and whether it is new.

        Re-discovering the same `(source_type, source_ref)` returns the existing row
        unchanged, also when two processes discover it at the same time.
        """
        stmt = (
            sqlite_insert(Service)
            .values(
                source_type=item.source_type,
                source_ref=item.source_ref,
                title=item.title,
                service_date=item.service_date,
                recorded_at=item.recorded_at,
                url=item.url,
                hints=dict(item.hints),
            )
            .on_conflict_do_nothing(index_elements=["source_type", "source_ref"])
            .returning(Service.id)
        )
        new_id = self.session.execute(stmt).scalar_one_or_none()
        if new_id is None:
            existing = self.by_source(item.source_type, item.source_ref)
            assert existing is not None
            return existing, False
        service = self.get(new_id)
        for position, part in enumerate(item.parts):
            service.parts.append(_part_row(position, part))
        self.session.flush()
        return service, True

    def set_part_included(self, part_id: int, included: bool) -> SourcePart:
        part = _get(self.session, SourcePart, part_id)
        part.included = included
        self.session.flush()
        return part

    @staticmethod
    def to_source_item(service: Service) -> SourceItem:
        return SourceItem(
            source_type=service.source_type,
            source_ref=service.source_ref,
            parts=tuple(
                SourcePartValue(
                    ref=p.ref,
                    name=p.name,
                    size=p.size,
                    checksum=p.checksum,
                    modified_at=p.modified_at,
                    mime=p.mime,
                )
                for p in service.parts
            ),
            title=service.title,
            service_date=service.service_date,
            recorded_at=service.recorded_at,
            url=service.url,
            hints=service.hints,
        )


def _part_row(position: int, part: SourcePartValue) -> SourcePart:
    return SourcePart(
        position=position,
        ref=part.ref,
        name=part.name,
        size=part.size,
        checksum=part.checksum,
        modified_at=part.modified_at,
        mime=part.mime,
    )


class ArtifactRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        service_id: int,
        kind: str,
        ref: ArtifactRef,
        *,
        storage_key: str,
        produced_by_run_id: int | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> Artifact:
        if kind not in ARTIFACT_KINDS:
            raise RepositoryError(f"unknown artifact kind {kind!r}")
        artifact = Artifact(
            service_id=service_id,
            kind=kind,
            storage_key=storage_key,
            sha256=ref.sha256,
            size=ref.size,
            mime=ref.mime,
            duration_ms=ref.duration_ms,
            produced_by_run_id=produced_by_run_id,
            meta=dict(meta or {}),
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def get(self, artifact_id: int) -> Artifact:
        return _get(self.session, Artifact, artifact_id)

    def for_service(self, service_id: int, kind: str | None = None) -> Sequence[Artifact]:
        stmt = select(Artifact).where(Artifact.service_id == service_id)
        if kind is not None:
            stmt = stmt.where(Artifact.kind == kind)
        return self.session.scalars(stmt.order_by(Artifact.id)).all()


class SermonRepository:
    """Sermons, their immutable revisions, and approvals.

    A sermon is approved exactly when an approval exists for its current revision,
    so adding a revision after approval leaves the sermon unapproved.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, service_id: int) -> Sermon:
        sermon = Sermon(service_id=service_id)
        self.session.add(sermon)
        self.session.flush()
        return sermon

    def get(self, sermon_id: int) -> Sermon:
        return _get(self.session, Sermon, sermon_id)

    def for_service(self, service_id: int) -> Sequence[Sermon]:
        stmt = select(Sermon).where(Sermon.service_id == service_id).order_by(Sermon.id)
        return self.session.scalars(stmt).all()

    def revision(self, revision_id: int) -> SermonRevision:
        return _get(self.session, SermonRevision, revision_id)

    def revisions(self, sermon_id: int) -> Sequence[SermonRevision]:
        stmt = (
            select(SermonRevision)
            .where(SermonRevision.sermon_id == sermon_id)
            .order_by(SermonRevision.number)
        )
        return self.session.scalars(stmt).all()

    def current_revision(self, sermon_id: int) -> SermonRevision | None:
        sermon = self.get(sermon_id)
        if sermon.current_revision_id is None:
            return None
        return self.revision(sermon.current_revision_id)

    def add_revision(
        self,
        sermon_id: int,
        *,
        author: str,
        boundaries: ServiceBoundaries,
        metadata: SermonMetadata,
        transcript_markdown: str | None = None,
        field_origins: Mapping[str, str] | None = None,
        sermon_audio_artifact_id: int | None = None,
    ) -> SermonRevision:
        """Insert a new revision, child of the current one, and make it current."""
        if author != "system" and not author.startswith("user:"):
            raise RepositoryError(f"author must be 'system' or 'user:<id>', not {author!r}")
        sermon = self.get(sermon_id)
        last = self.session.scalar(
            select(func.max(SermonRevision.number)).where(SermonRevision.sermon_id == sermon_id)
        )
        revision = SermonRevision(
            sermon_id=sermon_id,
            parent_revision_id=sermon.current_revision_id,
            number=(last or 0) + 1,
            author=author,
            service_start_ms=boundaries.service_start_ms,
            sermon_start_ms=boundaries.sermon_start_ms,
            sermon_end_ms=boundaries.sermon_end_ms,
            service_end_ms=boundaries.service_end_ms,
            sermon_metadata=metadata.model_dump(mode="json"),
            transcript_markdown=transcript_markdown,
            sermon_audio_artifact_id=sermon_audio_artifact_id,
            field_origins=dict(field_origins or {}),
            content_hash=content_hash(boundaries, metadata, transcript_markdown),
        )
        self.session.add(revision)
        self.session.flush()
        sermon.current_revision_id = revision.id
        self.session.flush()
        return revision

    def restore(self, revision_id: int, *, author: str) -> SermonRevision:
        """Make an older revision's content current again, as a new revision."""
        old = self.revision(revision_id)
        return self.add_revision(
            old.sermon_id,
            author=author,
            boundaries=self.boundaries(old),
            metadata=self.metadata(old),
            transcript_markdown=old.transcript_markdown,
            field_origins=old.field_origins,
            sermon_audio_artifact_id=old.sermon_audio_artifact_id,
        )

    def attach_sermon_audio(self, revision_id: int, artifact_id: int) -> SermonRevision:
        """Record the sermon audio cut for this revision; allowed once per revision."""
        revision = self.revision(revision_id)
        if revision.sermon_audio_artifact_id is not None:
            raise ImmutableRevisionError(f"revision {revision_id} already has sermon audio")
        revision.sermon_audio_artifact_id = artifact_id
        self.session.flush()
        return revision

    def approve(self, revision_id: int, *, approved_by: int, note: str | None = None) -> Approval:
        revision = self.revision(revision_id)
        if self.get(revision.sermon_id).current_revision_id != revision_id:
            raise NotCurrentRevisionError(f"revision {revision_id} is not the current revision")
        approval = Approval(revision_id=revision_id, approved_by=approved_by, note=note)
        self.session.add(approval)
        self.session.flush()
        return approval

    def current_approval(self, sermon_id: int) -> Approval | None:
        stmt = (
            select(Approval)
            .join(Sermon, Sermon.current_revision_id == Approval.revision_id)
            .where(Sermon.id == sermon_id)
            .order_by(Approval.id.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def approvals(self, sermon_id: int) -> Sequence[Approval]:
        """Every approval of any of the sermon's revisions, oldest first."""
        stmt = (
            select(Approval)
            .join(SermonRevision, SermonRevision.id == Approval.revision_id)
            .where(SermonRevision.sermon_id == sermon_id)
            .order_by(Approval.id)
        )
        return self.session.scalars(stmt).all()

    def is_approved(self, sermon_id: int) -> bool:
        return self.current_approval(sermon_id) is not None

    @staticmethod
    def boundaries(revision: SermonRevision) -> ServiceBoundaries:
        return ServiceBoundaries(
            service_start_ms=revision.service_start_ms,
            sermon_start_ms=revision.sermon_start_ms,
            sermon_end_ms=revision.sermon_end_ms,
            service_end_ms=revision.service_end_ms,
        )

    @staticmethod
    def metadata(revision: SermonRevision) -> SermonMetadata:
        return SermonMetadata.model_validate(revision.sermon_metadata)


class PublicationRepository:
    """One publication per (sermon, output target); retries update the same row."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, publication_id: int) -> Publication:
        return _get(self.session, Publication, publication_id)

    def find(self, sermon_id: int, target_name: str) -> Publication | None:
        stmt = select(Publication).where(
            Publication.sermon_id == sermon_id, Publication.target_name == target_name
        )
        return self.session.scalars(stmt).one_or_none()

    def for_sermon(self, sermon_id: int) -> Sequence[Publication]:
        stmt = select(Publication).where(Publication.sermon_id == sermon_id)
        return self.session.scalars(stmt.order_by(Publication.target_name)).all()

    def prepare(
        self, sermon_id: int, target_name: str, revision_id: int, publication_key: str
    ) -> Publication:
        """Create the publication, or point the existing one at `revision_id` again.

        The existing row keeps its publication key and external reference, so the
        output updates the object it created before.
        """
        revision = _get(self.session, SermonRevision, revision_id)
        if revision.sermon_id != sermon_id:
            raise RepositoryError(f"revision {revision_id} belongs to another sermon")
        now = utcnow()
        stmt = (
            sqlite_insert(Publication)
            .values(
                sermon_id=sermon_id,
                target_name=target_name,
                revision_id=revision_id,
                publication_key=publication_key,
                status="pending",
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["sermon_id", "target_name"],
                set_={
                    "revision_id": revision_id,
                    "status": "pending",
                    "last_error": None,
                    "updated_at": now,
                },
            )
            .returning(Publication.id)
        )
        publication_id = self.session.execute(stmt).scalar_one()
        publication = self.session.get(Publication, publication_id, populate_existing=True)
        assert publication is not None
        return publication

    def mark_publishing(self, publication_id: int) -> Publication:
        publication = self.get(publication_id)
        publication.status = "publishing"
        publication.attempts += 1
        self.session.flush()
        return publication

    def mark_published(self, publication_id: int, ref: ExternalRef) -> Publication:
        publication = self.get(publication_id)
        publication.status = "published"
        publication.external_id = ref.external_id
        publication.external_url = ref.url
        publication.last_error = None
        self.session.flush()
        return publication

    def mark_failed(self, publication_id: int, error: str) -> Publication:
        publication = self.get(publication_id)
        publication.status = "failed"
        publication.last_error = error
        self.session.flush()
        return publication

    @staticmethod
    def external_ref(publication: Publication) -> ExternalRef | None:
        if publication.external_id is None:
            return None
        return ExternalRef(
            target=publication.target_name,
            external_id=publication.external_id,
            url=publication.external_url,
        )
