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
        written_at = pub.published_at
        assert written_at is not None

        r2 = sermons.add_revision(
            sermon_id, author="user:1", boundaries=BOUNDS, metadata=META, transcript_markdown="t"
        )
        again = repo.prepare(sermon_id, "website", r2.id, "a-different-key")
        assert again.id == pub_id
        assert (again.status, again.revision_id) == ("pending", r2.id)
        assert again.publication_key == key
        repo.mark_publishing(again.id)
        assert again.published_at == written_at  # only a successful write moves it
        assert repo.external_ref(again) == ExternalRef(
            target="website", external_id="42", url="https://x/42", written_at=written_at
        )
        repo.mark_published(again.id, repo.external_ref(again))
        assert again.published_at == written_at  # published without a write: time kept
        assert again.published_revision_id == r2.id
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


def _sermon_with(
    s: Session, ref: str, when: date | None, *preachers: str, meta_date: date | None = None
) -> tuple[int, list[int]]:
    """A service dated `when` whose sermon has one revision per preacher name."""
    service, _ = ServiceRepository(s).add_discovered(
        ITEM.model_copy(update={"source_ref": ref, "service_date": when})
    )
    sermons = SermonRepository(s)
    sermon = sermons.create(service.id)
    revisions = [
        sermons.add_revision(
            sermon.id,
            author="system",
            boundaries=BOUNDS,
            metadata=SermonMetadata(preacher=name, service_date=meta_date),
        ).id
        for name in preachers
    ]
    return sermon.id, revisions


def test_preacher_suggestions_order_window_and_seeds(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as s:
        current, _ = _sermon_with(s, "now", date(2026, 11, 1), "Being Reviewed")
        _sermon_with(s, "e1", date(2026, 10, 25), "Pastor Eric")
        _sermon_with(s, "e2", date(2026, 10, 18), " Pastor Eric ")
        _sermon_with(s, "n1", date(2026, 11, 15), "Next Week")
        _sermon_with(s, "s1", date(2026, 10, 11), "Vicar Sarah")
        _sermon_with(s, "t1", date(2026, 8, 3), "Bishop Tom")  # exactly 90 days before
        for i, when in enumerate([date(2026, 3, 1), date(2026, 2, 1), date(2026, 1, 4)]):
            _sermon_with(s, f"o{i}", when, "Old Pastor")
        _sermon_with(s, "g1", date(2025, 12, 1), "Guest")
        _sermon_with(s, "g2", date(2025, 11, 1), "pastor eric")
        _sermon_with(s, "x1", date(2026, 10, 1), "   ")
        _sermon_with(s, "u1", None, "Undated")
        # The revision's own service date wins over the service's discovered date.
        _sermon_with(s, "m1", date(2026, 10, 30), "Moved", meta_date=date(2025, 1, 1))
    with sessions() as s:
        repo = SermonRepository(s)
        seeds = ["  Pastor Eric ", "The Rev. New", "", "The Rev. New", "Guest"]
        names = repo.preacher_suggestions(
            date(2026, 11, 1), recent_days=90, seeds=seeds, exclude_sermon_id=current
        )
        assert names == [
            "Pastor Eric", "Next Week", "Vicar Sarah", "Bishop Tom",
            "Old Pastor", "Guest", "pastor eric", "Moved", "Undated", "The Rev. New",
        ]  # fmt: skip
        # Relative to the reviewed service's date, not today.
        march = repo.preacher_suggestions(date(2026, 3, 15), recent_days=90)
        assert march[:2] == ["Old Pastor", "Pastor Eric"]  # Guest is 104 days before
        wider = repo.preacher_suggestions(date(2026, 3, 15), recent_days=110)
        assert wider[:2] == ["Old Pastor", "Guest"]
        assert "Being Reviewed" in march
        assert repo.preacher_suggestions(None, recent_days=90)[0] == "Old Pastor"
        assert repo.preacher_suggestions(date(2026, 11, 1), recent_days=0)[0] == "Being Reviewed"


def test_preacher_suggestions_prefer_approved_then_published(
    sessions: sessionmaker[Session], user_id: int
) -> None:
    with sessions.begin() as s:
        approved, (first, _) = _sermon_with(s, "a", date(2026, 10, 25), "Approved", "Draft A")
        published, (pub_rev, _) = _sermon_with(s, "p", date(2026, 10, 18), "Published", "Draft P")
        _sermon_with(s, "c", date(2026, 10, 11), "Current Only")
    with sessions.begin() as s:
        sermons = SermonRepository(s)
        # Approve the first revision by making it current again for a moment.
        restored = sermons.restore(first, author="system")
        sermons.approve(restored.id, approved_by=user_id)
        sermons.add_revision(
            approved, author="system", boundaries=BOUNDS, metadata=SermonMetadata(preacher="Later")
        )
        pubs = PublicationRepository(s)
        pub = pubs.prepare(published, "website", pub_rev, "key-p")
        pubs.mark_published(pub.id, ExternalRef(target="website", external_id="1"))
    with sessions() as s:
        names = SermonRepository(s).preacher_suggestions(date(2026, 11, 1), recent_days=90)
    assert names == ["Approved", "Published", "Current Only"]
