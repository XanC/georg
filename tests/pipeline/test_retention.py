# SPDX-License-Identifier: AGPL-3.0-only
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import Engine

from georg.core.config import GeorgConfig
from georg.core.types import SermonMetadata, ServiceBoundaries, SourceItem, SourcePart
from georg.db.engine import session_factory
from georg.db.models import Service
from georg.db.repositories import (
    ArtifactRepository,
    AuditRepository,
    PublicationRepository,
    SermonRepository,
    ServiceRepository,
)
from georg.db.types import utcnow
from georg.jobs.queue import JobQueue
from georg.pipeline.retention import cleanup, maybe_enqueue_cleanup
from georg.storage.local import LocalStore

BOUNDS = ServiceBoundaries(
    service_start_ms=0, sermon_start_ms=1000, sermon_end_ms=2000, service_end_ms=3000
)


def _config(tmp_path: Path, days: int = 60) -> GeorgConfig:
    return GeorgConfig.model_validate({
        "instance": "test", "data_dir": tmp_path / "data", "retention": {"days": days},
        "inputs": [{"name": "local", "type": "local_folder", "options": {"root": tmp_path}}],
        "processors": {k: {"type": "unused"} for k in
                       ("classifier", "detector", "transcriber", "formatter", "metadata")},
    })  # fmt: skip


def _service(
    engine: Engine, config: GeorgConfig, ref: str, *, age_days: int, content: bytes,
    published_days: int | None = None, cache: str | None = None,
) -> int:  # fmt: skip
    store = LocalStore.for_data_dir(config.data_dir)
    src = config.data_dir / f"{ref}.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(content)
    ref_ = store.put(src, mime="audio/mpeg")
    item = SourceItem(
        source_type="local_folder", source_ref=ref, title=ref, service_date=date(2026, 9, 6),
        parts=(SourcePart(ref=f"{ref}.wav", name=f"{ref}.wav", size=1, checksum=ref),),
    )  # fmt: skip
    with session_factory(engine).begin() as s:
        service, _ = ServiceRepository(s).add_discovered(item)
        service.discovered_at = utcnow() - timedelta(days=age_days)
        audio = ArtifactRepository(s).add(service.id, "sermon_audio", ref_, storage_key=ref_.sha256)
        ArtifactRepository(s).add(
            service.id, "rendition", ref_, storage_key=ref_.sha256, meta={"name": "preview"}
        )
        sermons = SermonRepository(s)
        sermon = sermons.create(service.id)
        sermons.add_revision(
            sermon.id, author="system", boundaries=BOUNDS, metadata=SermonMetadata(title=ref),
            transcript_markdown="t", sermon_audio_artifact_id=audio.id,
        )  # fmt: skip
        if published_days is not None:
            revision = sermons.current_revision(sermon.id)
            assert revision is not None
            pub = PublicationRepository(s).prepare(sermon.id, "web", revision.id, f"k-{ref}")
            pub.status, pub.external_id = "published", "8216"
            pub.published_at = utcnow() - timedelta(days=published_days)
        if cache:
            path = config.data_dir / "cache" / cache
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"cached")
            service.cache_entries = [cache]
        return service.id


def test_purges_on_both_clocks_and_keeps_rows(tmp_path: Path, engine: Engine) -> None:
    config = _config(tmp_path)
    store = LocalStore.for_data_dir(config.data_dir)
    old_unpublished = _service(engine, config, "a", age_days=61, content=b"a", cache="audio/a.flac")
    pub_old = _service(engine, config, "b", age_days=200, content=b"b", published_days=61)
    pub_new = _service(engine, config, "c", age_days=200, content=b"c", published_days=59)
    young = _service(engine, config, "d", age_days=59, content=b"d")
    sessions = session_factory(engine)

    dry = cleanup(config, sessions, JobQueue(engine), dry_run=True)
    assert sorted(p.service_id for p in dry.purges) == [old_unpublished, pub_old]
    assert {p.reason for p in dry.purges} == {"discovered", "published"}
    assert all(store.location(p.files[0].name).is_file() for p in dry.purges)

    report = cleanup(config, sessions, JobQueue(engine))
    assert report.bytes == dry.bytes > 0
    assert not (config.data_dir / "cache" / "audio" / "a.flac").exists()
    with sessions() as s:
        purged = s.get(Service, pub_old)
        assert purged is not None and purged.purged_at is not None
        assert PublicationRepository(s).for_sermon(SermonRepository(s).for_service(purged.id)[0].id)
        assert ArtifactRepository(s).for_service(purged.id, "rendition") == []
        assert len(ArtifactRepository(s).for_service(purged.id, "sermon_audio")) == 1
        for keep in (pub_new, young):
            service = s.get(Service, keep)
            assert service is not None and service.purged_at is None
        actions = [(e.action, e.target) for e in AuditRepository(s).recent()]
    assert ("service.purge", f"service {old_unpublished}") in actions
    assert cleanup(config, sessions, JobQueue(engine)).purges == []


def test_shared_files_and_busy_services_are_kept(tmp_path: Path, engine: Engine) -> None:
    config = _config(tmp_path)
    store = LocalStore.for_data_dir(config.data_dir)
    old = _service(engine, config, "old", age_days=90, content=b"same", cache="audio/x.flac")
    _service(engine, config, "new", age_days=1, content=b"same", cache="audio/x.flac")
    busy = _service(engine, config, "busy", age_days=90, content=b"busy")
    queue = JobQueue(engine)
    queue.enqueue("process", {"service_id": busy, "reprocess": True})
    report = cleanup(config, session_factory(engine), queue)
    assert [p.service_id for p in report.purges] == [old]
    assert report.purges[0].files == []
    assert report.skipped_busy == [busy]
    assert (config.data_dir / "cache" / "audio" / "x.flac").is_file()
    assert any(p.name == store.location(p.name).name for p in store.root.rglob("*") if p.is_file())


def test_retention_off_and_daily_job(tmp_path: Path, engine: Engine) -> None:
    off = _config(tmp_path, days=0)
    _service(engine, off, "a", age_days=400, content=b"a")
    queue = JobQueue(engine)
    assert cleanup(off, session_factory(engine), queue).purges == []
    assert maybe_enqueue_cleanup(off, queue) is None
    on = _config(tmp_path)
    assert maybe_enqueue_cleanup(on, queue) is not None
    assert maybe_enqueue_cleanup(on, queue) is None


def test_discovery_does_not_fetch_a_purged_service_again(tmp_path: Path, engine: Engine) -> None:
    config = _config(tmp_path)
    service_id = _service(engine, config, "a", age_days=90, content=b"a")
    cleanup(config, session_factory(engine), JobQueue(engine))
    item = SourceItem(
        source_type="local_folder", source_ref="a", title="a",
        parts=(SourcePart(ref="a.wav", name="a.wav", size=1, checksum="a"),),
    )  # fmt: skip
    with session_factory(engine).begin() as s:
        service, new = ServiceRepository(s).add_discovered(item)
        assert not new and service.id == service_id and service.purged_at is not None
