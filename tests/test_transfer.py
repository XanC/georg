# SPDX-License-Identifier: AGPL-3.0-only
"""`georg export` / `georg import` / `georg resume` (fakes only, no network)."""

import json
import sqlite3
import tarfile
import types
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from georg.core.config import GeorgConfig
from georg.core.errors import GeorgError, ModuleError
from georg.core.types import (
    ExternalRef,
    PublicationRequest,
    SermonMetadata,
    ServiceBoundaries,
    SourceItem,
    SourcePart,
)
from georg.db.engine import create_engine, database_path, session_factory
from georg.db.migrate import upgrade
from georg.db.models import Job, Publication
from georg.db.repositories import (
    ArtifactRepository,
    IdentityRepository,
    SermonRepository,
    ServiceRepository,
)
from georg.jobs.queue import JobQueue
from georg.pipeline.service import discover, publish_sermon
from georg.storage.local import LocalStore
from georg.transfer import export_bundle, import_bundle, resume


def make_config(data_dir: Path, input_root: Path) -> GeorgConfig:
    return GeorgConfig.model_validate({
        "instance": "test",
        "data_dir": data_dir,
        "inputs": [{"name": "local", "type": "local_folder",
                    "options": {"root": input_root}}],
        "processors": {k: {"type": "unused"} for k in
                       ("classifier", "detector", "transcriber", "formatter", "metadata")},
        "outputs": [{"name": "website", "type": "wordpress"}],
        "review": {"require_approval": False},
    })  # fmt: skip


def make_service(engine: Engine, config: GeorgConfig, sermon_audio: Path) -> tuple[int, int]:
    """One service with a sermon, a revision, and a stored sermon-audio artifact."""
    item = SourceItem(
        source_type="local_folder",
        source_ref=str(config.data_dir / "recordings" / "SPX 11.01.26"),
        parts=(
            SourcePart(
                ref=str(config.data_dir / "recordings" / "SPX 11.01.26" / "1.wav"),
                name="1.wav",
            ),
        ),
        title="All Saints",
        service_date=date(2026, 11, 1),
        recorded_at=datetime(2026, 11, 1, 18, 0, tzinfo=UTC),
    )
    with session_factory(engine).begin() as s:
        service, _ = ServiceRepository(s).add_discovered(item)
        ref = LocalStore.for_data_dir(config.data_dir).put(sermon_audio, mime="audio/mpeg")
        artifact = ArtifactRepository(s).add(
            service.id, "sermon_audio", ref, storage_key=ref.sha256, meta={"name": "sermon"}
        )
        sermons = SermonRepository(s)
        sermon = sermons.create(service.id)
        revision = sermons.add_revision(
            sermon.id,
            author="system",
            boundaries=ServiceBoundaries(
                service_start_ms=0,
                sermon_start_ms=1000,
                sermon_end_ms=9000,
                service_end_ms=10000,
            ),
            metadata=SermonMetadata(title="All Saints", service_date=date(2026, 11, 1)),
            sermon_audio_artifact_id=artifact.id,
        )
        return sermon.id, revision.id


class FakeTarget:
    """An output that counts creates vs updates and answers find_existing."""

    def __init__(self, existing: ExternalRef | None = None, reachable: bool = True) -> None:
        self.name = "website"
        self.renditions = frozenset()
        self.wants_transcript = False
        self.creates = 0
        self.updates = 0
        self.existing = existing
        self.reachable = reachable
        self.requests: list[PublicationRequest] = []

    def find_existing(self, publication_key: str) -> ExternalRef | None:
        if not self.reachable:
            raise ConnectionError("target down")
        return self.existing

    def publish(self, request: PublicationRequest) -> ExternalRef:
        self.requests.append(request)
        if request.existing is not None:
            self.updates += 1
            return request.existing
        self.creates += 1
        return ExternalRef(target=self.name, external_id=f"post-{self.creates}")


@pytest.fixture
def exported(tmp_path: Path):
    """A source instance with one published sermon, exported to a bundle."""
    src_dir = tmp_path / "src" / "data"
    input_root = tmp_path / "src" / "recordings"
    input_root.mkdir(parents=True)
    audio = tmp_path / "sermon.mp3"
    audio.write_bytes(b"fake mp3 bytes")
    config = make_config(src_dir, input_root)
    db_path = database_path(src_dir)
    upgrade(db_path)
    engine = create_engine(db_path)
    sermon_id, _ = make_service(engine, config, audio)
    sessions = session_factory(engine)
    target = FakeTarget()
    statuses = publish_sermon(config, sessions, sermon_id, outputs=[target])
    assert statuses == {"website": "published"} and target.creates == 1
    bundle = tmp_path / "church.georg"
    report = export_bundle(config, engine, db_path, bundle)
    return types.SimpleNamespace(
        config=config,
        engine=engine,
        sessions=sessions,
        sermon_id=sermon_id,
        bundle=bundle,
        report=report,
        tmp_path=tmp_path,
    )


def test_export_freezes_source(exported) -> None:
    with exported.sessions() as s:
        assert IdentityRepository(s).state() == "frozen"
    with pytest.raises(ModuleError, match="frozen"):
        publish_sermon(exported.config, exported.sessions, exported.sermon_id, outputs=[])
    with pytest.raises(ModuleError, match="frozen"):
        discover(exported.config, exported.sessions, JobQueue(exported.engine))


def test_import_roundtrip(exported) -> None:
    dst = exported.tmp_path / "elsewhere" / "georg-data"
    new_config = make_config(dst, exported.tmp_path / "new" / "recordings")
    with exported.sessions() as s:
        church_id = IdentityRepository(s).church_id()

    report = import_bundle(new_config, exported.bundle)
    assert report.church_id == church_id
    assert report.artifacts_restored == 1

    engine = create_engine(database_path(dst))
    try:
        with session_factory(engine)() as s:
            identity = IdentityRepository(s)
            assert identity.church_id() == church_id
            assert identity.state() == "paused"
            # The data_dir path in the source ref was remapped to the new layout.
            [service] = ServiceRepository(s).newest_first()
            assert str(dst) in service.source_ref
            assert str(exported.config.data_dir) not in service.source_ref
            [pub] = s.scalars(select(Publication)).all()
            assert pub.status == "published" and pub.external_id == "post-1"
            assert not s.scalars(select(Job)).all()
        # The artifact landed in the new store.
        shas = [a.sha256 for a in ArtifactRepository(session_factory(engine)()).for_service(1)]
        assert all(LocalStore.for_data_dir(dst).exists(sha) for sha in shas)
    finally:
        engine.dispose()


def test_imported_publish_updates_existing_object(exported) -> None:
    dst = exported.tmp_path / "elsewhere" / "georg-data"
    new_config = make_config(dst, exported.tmp_path / "new" / "recordings")
    import_bundle(new_config, exported.bundle)
    engine = create_engine(database_path(dst))
    try:
        sessions = session_factory(engine)
        # Paused: publishing is refused before resume.
        with pytest.raises(ModuleError, match="paused"):
            publish_sermon(new_config, sessions, exported.sermon_id, outputs=[FakeTarget()])

        # Resume verifies the published object at its target.
        target = FakeTarget(existing=ExternalRef(target="website", external_id="post-1"))
        report = resume(new_config, sessions, targets={"website": target})
        assert report.resumed and report.problems == 0

        # A new revision approved later republishes as an update, not a create.
        with sessions.begin() as s:
            sermons = SermonRepository(s)
            current = sermons.current_revision(exported.sermon_id)
            sermons.add_revision(
                exported.sermon_id,
                author="user:1",
                boundaries=sermons.boundaries(current),
                metadata=sermons.metadata(current),
                transcript_markdown="edited",
            )
        statuses = publish_sermon(new_config, sessions, exported.sermon_id, outputs=[target])
        assert statuses == {"website": "published"}
        assert target.creates == 0 and target.updates == 1
        request = target.requests[0]
        assert request.existing is not None and request.existing.external_id == "post-1"
        with sessions() as s:
            [pub] = s.scalars(select(Publication)).all()
            assert pub.published_at is not None and pub.external_id == "post-1"
    finally:
        engine.dispose()


def test_resume_refuses_on_missing_publication(exported) -> None:
    dst = exported.tmp_path / "elsewhere" / "georg-data"
    new_config = make_config(dst, exported.tmp_path / "new" / "recordings")
    import_bundle(new_config, exported.bundle)
    engine = create_engine(database_path(dst))
    try:
        sessions = session_factory(engine)
        report = resume(new_config, sessions, targets={"website": FakeTarget()})
        assert not report.resumed and len(report.missing) == 1
        report = resume(new_config, sessions, force=True, targets={"website": FakeTarget()})
        assert report.resumed
    finally:
        engine.dispose()


def test_import_refuses_newer_georg(exported, tmp_path: Path) -> None:
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    db = bad_dir / "db.sqlite3"
    sqlite3.connect(db).close()
    manifest = {
        "format": 1,
        "alembic_revision": "9999",
        "files": {"db.sqlite3": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
    }
    (bad_dir / "manifest.json").write_text(json.dumps(manifest))
    bundle = tmp_path / "newer.georg"
    with tarfile.open(bundle, "w") as tar:
        tar.add(bad_dir / "manifest.json", arcname="manifest.json")
        tar.add(db, arcname="db.sqlite3")
    with pytest.raises(GeorgError, match="newer Georg"):
        import_bundle(make_config(tmp_path / "dst", tmp_path / "in"), bundle)


def test_import_refuses_existing_database(exported) -> None:
    with pytest.raises(GeorgError, match="already exists"):
        import_bundle(exported.config, exported.bundle)


def test_frozen_instance_cannot_export_again(exported) -> None:
    with pytest.raises(GeorgError, match="already frozen"):
        export_bundle(
            exported.config,
            exported.engine,
            database_path(exported.config.data_dir),
            exported.tmp_path / "second.georg",
        )
