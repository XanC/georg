# SPDX-License-Identifier: AGPL-3.0-only
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from georg.core.types import (
    ArtifactRef,
    ExternalRef,
    SermonMetadata,
    ServiceBoundaries,
    SourceItem,
    SourcePart,
)
from georg.db import session_factory
from georg.db.models import Approval, Artifact, Publication, Sermon
from georg.db.repositories import (
    ArtifactRepository,
    ImmutableRevisionError,
    NotCurrentRevisionError,
    PublicationRepository,
    RepositoryError,
    SermonRepository,
    ServiceRepository,
    UserRepository,
)

ITEM = SourceItem(
    source_type="google_drive",
    source_ref="folder-123",
    parts=(
        SourcePart(ref="file-a", name="part1.mp4", size=10, checksum="aa"),
        SourcePart(ref="file-b", name="part2.mp4", size=20, checksum="bb"),
    ),
    title="SPX 11.01.26",
    service_date=date(2026, 11, 1),
    hints={"documents": ["2026-11-01_All_Saints__Day_-_DS1-Eric.docx"]},
)
BOUNDS = ServiceBoundaries(
    service_start_ms=1_000,
    sermon_start_ms=1_200_000,
    sermon_end_ms=2_400_000,
    service_end_ms=3_600_000,
)
META = SermonMetadata(title="Saints", preacher="Eric", scripture=("Matthew 5:1-12",))


@pytest.fixture
def sessions(engine: Engine) -> sessionmaker[Session]:
    return session_factory(engine)


@pytest.fixture
def sermon_id(sessions: sessionmaker[Session]) -> int:
    with sessions.begin() as s:
        service, _ = ServiceRepository(s).add_discovered(ITEM)
        return SermonRepository(s).create(service.id).id


@pytest.fixture
def user_id(sessions: sessionmaker[Session]) -> int:
    with sessions.begin() as s:
        return UserRepository(s).add("reviewer", "argon2-hash", "reviewer").id


def _artifact(s: Session, service_id: int, sha: str = "a" * 64) -> int:
    ref = ArtifactRef(path=Path("/x"), sha256=sha, size=3, mime="audio/mpeg", duration_ms=5)
    return ArtifactRepository(s).add(service_id, "sermon_audio", ref, storage_key=sha).id


def test_discovered_service_is_deduplicated(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as s:
        first, created = ServiceRepository(s).add_discovered(ITEM)
        first_id = first.id
    assert created
    with sessions.begin() as s:
        repo = ServiceRepository(s)
        again, created = repo.add_discovered(ITEM.model_copy(update={"title": "changed"}))
        assert not created
        assert again.id == first_id
        assert again.title == "SPX 11.01.26"
        assert [p.ref for p in again.parts] == ["file-a", "file-b"]
        assert repo.to_source_item(again) == ITEM


def test_source_part_can_be_excluded(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as s:
        service, _ = ServiceRepository(s).add_discovered(ITEM)
        part_id = service.parts[1].id
    with sessions.begin() as s:
        assert not ServiceRepository(s).set_part_included(part_id, False).included


def test_revisions_are_numbered_and_linked(sessions: sessionmaker[Session], sermon_id: int) -> None:
    with sessions.begin() as s:
        repo = SermonRepository(s)
        r1 = repo.add_revision(sermon_id, author="system", boundaries=BOUNDS, metadata=META)
        r2 = repo.add_revision(
            sermon_id,
            author="user:1",
            boundaries=BOUNDS,
            metadata=META.model_copy(update={"title": "All Saints"}),
            field_origins={"title": "edited"},
        )
        assert (r1.number, r2.number) == (1, 2)
        assert r2.parent_revision_id == r1.id
        assert r1.content_hash != r2.content_hash
        assert repo.current_revision(sermon_id) == r2
        assert repo.boundaries(r2) == BOUNDS
        assert repo.metadata(r2).title == "All Saints"
        with pytest.raises(RepositoryError):
            repo.add_revision(sermon_id, author="bob", boundaries=BOUNDS, metadata=META)


def test_revisions_are_immutable(sessions: sessionmaker[Session], sermon_id: int) -> None:
    with sessions.begin() as s:
        rev_id = (
            SermonRepository(s)
            .add_revision(sermon_id, author="system", boundaries=BOUNDS, metadata=META)
            .id
        )
    with pytest.raises(IntegrityError, match="immutable"), sessions.begin() as s:
        SermonRepository(s).revision(rev_id).transcript_markdown = "edited in place"
    with pytest.raises(IntegrityError, match="immutable"), sessions.begin() as s:
        s.execute(text("UPDATE sermon_revisions SET sermon_end_ms = 3000000"))


def test_sermon_audio_attaches_once(sessions: sessionmaker[Session], sermon_id: int) -> None:
    with sessions.begin() as s:
        service_id = SermonRepository(s).get(sermon_id).service_id
        rev_id = (
            SermonRepository(s)
            .add_revision(sermon_id, author="system", boundaries=BOUNDS, metadata=META)
            .id
        )
        first, second = _artifact(s, service_id), _artifact(s, service_id, "b" * 64)
    with sessions.begin() as s:
        assert SermonRepository(s).attach_sermon_audio(rev_id, first).sermon_audio_artifact_id
    with pytest.raises(ImmutableRevisionError), sessions.begin() as s:
        SermonRepository(s).attach_sermon_audio(rev_id, second)
    with pytest.raises(IntegrityError, match="immutable"), sessions.begin() as s:
        s.execute(text("UPDATE sermon_revisions SET sermon_audio_artifact_id = :a"), {"a": second})


def test_new_revision_invalidates_approval(
    sessions: sessionmaker[Session], sermon_id: int, user_id: int
) -> None:
    with sessions.begin() as s:
        repo = SermonRepository(s)
        r1 = repo.add_revision(sermon_id, author="system", boundaries=BOUNDS, metadata=META)
        assert not repo.is_approved(sermon_id)
        approval = repo.approve(r1.id, approved_by=user_id, note="ok")
        assert repo.is_approved(sermon_id)
        assert repo.current_approval(sermon_id) == approval

        repo.add_revision(
            sermon_id,
            author=f"user:{user_id}",
            boundaries=BOUNDS,
            metadata=META,
            transcript_markdown="Edited after approval.",
        )
        assert not repo.is_approved(sermon_id)
        assert repo.current_approval(sermon_id) is None
        with pytest.raises(NotCurrentRevisionError):
            repo.approve(r1.id, approved_by=user_id)

        restored = repo.restore(r1.id, author=f"user:{user_id}")
        assert restored.number == 3
        assert restored.content_hash == r1.content_hash
        assert not repo.is_approved(sermon_id)
        repo.approve(restored.id, approved_by=user_id)
        assert repo.is_approved(sermon_id)
        assert [a.revision_id for a in repo.approvals(sermon_id)] == [r1.id, restored.id]
        r1_id = r1.id

    with pytest.raises(IntegrityError, match="current revision"), sessions.begin() as s:
        s.add(Approval(revision_id=r1_id, approved_by=user_id))
    with pytest.raises(IntegrityError, match="immutable"), sessions.begin() as s:
        s.execute(text("UPDATE approvals SET note = 'changed'"))


def test_current_revision_must_belong_to_sermon(
    sessions: sessionmaker[Session], sermon_id: int
) -> None:
    with sessions.begin() as s:
        repo = SermonRepository(s)
        other = repo.create(repo.get(sermon_id).service_id)
        rev_id = repo.add_revision(sermon_id, author="system", boundaries=BOUNDS, metadata=META).id
        other_id = other.id
    with pytest.raises(IntegrityError, match="another sermon"), sessions.begin() as s:
        s.get_one(Sermon, other_id).current_revision_id = rev_id


def test_foreign_keys_are_enforced(sessions: sessionmaker[Session]) -> None:
    with pytest.raises(IntegrityError), sessions.begin() as s:
        s.add(
            Artifact(
                service_id=999, kind="segments", storage_key="k", sha256="c" * 64, size=1, mime="x"
            )
        )
        s.flush()


def test_publication_is_unique_per_target(sessions: sessionmaker[Session], sermon_id: int) -> None:
    key = "georg:test:service:1:website"
    with sessions.begin() as s:
        sermons = SermonRepository(s)
        r1 = sermons.add_revision(sermon_id, author="system", boundaries=BOUNDS, metadata=META)
        repo = PublicationRepository(s)
        pub = repo.prepare(sermon_id, "website", r1.id, key)
        assert (pub.status, pub.revision_id, pub.attempts) == ("pending", r1.id, 0)
        repo.mark_publishing(pub.id)
        repo.mark_published(
            pub.id, ExternalRef(target="website", external_id="42", url="https://x/42")
        )
        pub_id = pub.id

        r2 = sermons.add_revision(
            sermon_id, author="user:1", boundaries=BOUNDS, metadata=META, transcript_markdown="t"
        )
        again = repo.prepare(sermon_id, "website", r2.id, "a-different-key")
        assert again.id == pub_id
        assert (again.status, again.revision_id) == ("pending", r2.id)
        assert again.publication_key == key
        assert repo.external_ref(again) == ExternalRef(
            target="website", external_id="42", url="https://x/42"
        )
        assert [p.id for p in repo.for_sermon(sermon_id)] == [pub_id]
        r2_id = r2.id

    with pytest.raises(IntegrityError), sessions.begin() as s:
        s.add(
            Publication(
                sermon_id=sermon_id, target_name="website", revision_id=r2_id, publication_key="k2"
            )
        )


def test_services_newest_first(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as s:
        repo = ServiceRepository(s)
        for ref, day in [
            ("old", date(2026, 10, 25)),
            ("undated", None),
            ("new", date(2026, 11, 1)),
        ]:
            repo.add_discovered(ITEM.model_copy(update={"source_ref": ref, "service_date": day}))
        assert [svc.source_ref for svc in repo.newest_first()] == ["new", "old", "undated"]
        assert [svc.source_ref for svc in repo.newest_first(limit=1)] == ["new"]
