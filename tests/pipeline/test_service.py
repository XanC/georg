# SPDX-License-Identifier: AGPL-3.0-only
import dataclasses
import os
import time
from datetime import date, datetime
from pathlib import Path

import pytest
from docx_fixture import make_docx
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from test_local_run import PROPOSED, processors, tone

from georg.core.config import GeorgConfig, OutputConfig
from georg.core.errors import ModuleError
from georg.core.types import ExternalRef, PublicationRequest
from georg.db.engine import session_factory
from georg.db.models import Artifact
from georg.db.repositories import (
    ArtifactRepository,
    PublicationRepository,
    SermonRepository,
    ServiceRepository,
    UserRepository,
)
from georg.jobs.queue import JobQueue
from georg.media.ffmpeg import probe
from georg.modules.processors.metadata import RulesMetadataGenerator
from georg.pipeline import Worker, discover, process_service, publish_sermon, render_revision
from georg.pipeline.service import _renditions_for
from georg.storage.local import LocalStore


@pytest.fixture
def config(tmp_path: Path) -> GeorgConfig:
    folder = tmp_path / "drive" / "SPX 11.01.26"
    folder.mkdir(parents=True)
    tone(folder / "1.wav", 5, 440)
    tone(folder / "2.wav", 7, 660)
    (folder / "2026-11-01_All_Saints__Day_-_DS1-Eric.docx").write_bytes(b"")
    old = time.time() - 3600
    for f in folder.iterdir():
        os.utime(f, (old, old))
    return GeorgConfig.model_validate({
        "instance": "test",
        "data_dir": tmp_path / "data",
        "inputs": [{"name": "drive", "type": "local_folder",
                    "options": {"root": tmp_path / "drive", "folder_pattern": "SPX *"}}],
        "processors": {k: {"type": "unused"} for k in
                       ("classifier", "detector", "transcriber", "formatter", "metadata")},
    })  # fmt: skip


def test_discover_then_worker_proposes_a_revision(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    queue = JobQueue(engine)
    [service_id] = discover(config, sessions, queue)
    assert discover(config, sessions, queue) == []

    worker = Worker(config, engine, queue=queue, processors=processors(), worker_id="w")
    assert worker.run_once()
    assert not worker.run_once()
    assert queue.list()[0].status == "succeeded"

    with sessions() as s:
        [sermon] = SermonRepository(s).for_service(service_id)
        revision = SermonRepository(s).current_revision(sermon.id)
        assert revision is not None
        assert revision.author == "system"
        assert SermonRepository.boundaries(revision) == PROPOSED
        assert SermonRepository.metadata(revision).occasion == (
            "2026-11-01_All_Saints__Day_-_DS1-Eric.docx"
        )
        assert revision.transcript_markdown is not None
        assert "Grace to you." in revision.transcript_markdown
        kinds = sorted(a.kind for a in ArtifactRepository(s).for_service(service_id))
        assert kinds == ["boundary_proposal", *["rendition"] * 5,
                         "sermon_audio", "transcript_formatted"]  # fmt: skip
        cuts = cut_previews(s, service_id)
        assert sorted(cuts) == ["sermon_preview", "service_preview"]
        assert cuts["sermon_preview"].meta == {
            "name": "sermon_preview", "span": "sermon", "preview": True,
            "boundaries": [1_000, 4_000, 8_000, 11_000],
        }  # fmt: skip
        assert abs((cuts["sermon_preview"].duration_ms or 0) - 4_000) < 200
        assert abs((cuts["service_preview"].duration_ms or 0) - 10_000) < 200
        info = probe(store_path(config, cuts["service_preview"]))
        assert (info.channels, info.sample_rate) == (1, 22050)
        [preview] = previews(s, service_id)
        assert preview.meta == {"name": "preview", "span": "recording"}
        assert abs((preview.duration_ms or 0) - 12_000) < 200
        assert revision.sermon_audio_artifact_id is not None
        service = ServiceRepository(s).get(service_id)
        assert service.status_summary == "awaiting review"
        assert service.duration_ms is not None and service.duration_ms > 11_000

    # A retry after the proposal was saved changes nothing.
    assert process_service(config, sessions, service_id, processors()) is None


def test_reprocess_adds_a_second_system_revision(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    sermon_id, first_id = processed(config, engine)
    with sessions() as s:
        service_id = SermonRepository(s).get(sermon_id).service_id

    new_id = process_service(config, sessions, service_id, processors(), reprocess=True)
    assert new_id is not None and new_id != first_id
    with sessions() as s:
        sermons = SermonRepository(s)
        [first, current] = sermons.revisions(sermon_id)
        assert current.id == new_id
        assert current.author == "system"
        assert current.number == 2
        assert current.parent_revision_id == first.id
        assert sermons.get(sermon_id).current_revision_id == new_id
        assert ServiceRepository(s).get(service_id).status_summary == "awaiting review"


def test_reprocess_is_refused_once_published(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    sermon_id, revision_id = processed(config, engine)
    with sessions.begin() as s:
        service_id = SermonRepository(s).get(sermon_id).service_id
        PublicationRepository(s).prepare(sermon_id, "website", revision_id, "georg:test:k")

    with pytest.raises(ModuleError, match="publication") as err:
        process_service(config, sessions, service_id, processors(), reprocess=True)
    assert not err.value.retryable


def test_worker_reruns_a_reprocess_job(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    queue = JobQueue(engine)
    [service_id] = discover(config, sessions, queue)
    worker = Worker(config, engine, queue=queue, processors=processors(), worker_id="w")
    assert worker.run_once()

    queue.enqueue("process", {"service_id": service_id, "reprocess": True})
    assert worker.run_once()
    with sessions() as s:
        [sermon] = SermonRepository(s).for_service(service_id)
        assert len(SermonRepository(s).revisions(sermon.id)) == 2


def test_discover_respects_configured_start_date(config: GeorgConfig, engine: Engine) -> None:
    from georg.core.config import DiscoveryConfig

    old_folder = config.inputs[0].options["root"] / "SPX 10.25.26"
    old_folder.mkdir()
    tone(old_folder / "1.wav", 5, 440)
    old = time.time() - 3600
    os.utime(old_folder / "1.wav", (old, old))
    config = config.model_copy(update={"discovery": DiscoveryConfig(start_date=date(2026, 11, 1))})
    sessions = session_factory(engine)

    # The configured floor applies on its own, and still clamps an earlier `since`.
    [service_id] = discover(config, sessions, JobQueue(engine))
    with sessions() as s:
        assert ServiceRepository(s).get(service_id).service_date == date(2026, 11, 1)
    assert discover(config, sessions, JobQueue(engine), since=datetime(2026, 10, 1)) == []
    assert discover(config, sessions, JobQueue(engine), since=datetime(2026, 11, 2)) == []


def test_failed_job_is_recorded(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    queue = JobQueue(engine)
    [service_id] = discover(config, sessions, queue)
    with sessions.begin() as s:
        ServiceRepository(s).get(service_id).hints = {"input": "gone"}
    worker = Worker(config, engine, queue=queue, processors=processors(), worker_id="w")
    assert worker.run_once()
    [job] = queue.list()
    assert job.status == "failed"
    assert job.last_error is not None and "gone" in job.last_error


class FakeOutput:
    def __init__(self, name: str, renditions: set[str], fail: Exception | None = None) -> None:
        self.name, self.renditions, self.fail = name, frozenset(renditions), fail
        self.wants_transcript = True
        self.requests: list[PublicationRequest] = []

    def find_existing(self, publication_key: str) -> ExternalRef | None:
        return None

    def publish(self, request: PublicationRequest) -> ExternalRef:
        self.requests.append(request)
        if self.fail:
            raise self.fail
        assert all(ref.path.is_file() for ref in request.files.values())
        return ExternalRef(target=self.name, external_id=f"id-{len(self.requests)}")


def processed(config: GeorgConfig, engine: Engine) -> tuple[int, int]:
    sessions = session_factory(engine)
    [service_id] = discover(config, sessions, JobQueue(engine))
    revision_id = process_service(config, sessions, service_id, processors())
    assert revision_id is not None
    with sessions() as s:
        [sermon] = SermonRepository(s).for_service(service_id)
        return sermon.id, revision_id


def previews(s: Session, service_id: int) -> list[Artifact]:
    return [a for a in ArtifactRepository(s).for_service(service_id)
            if a.meta.get("name") == "preview"]  # fmt: skip


def cut_previews(s: Session, service_id: int) -> dict[str, Artifact]:
    return {str(a.meta["name"]): a for a in ArtifactRepository(s).for_service(service_id)
            if a.meta.get("preview")}  # fmt: skip


def store_path(config: GeorgConfig, artifact: Artifact) -> Path:
    return LocalStore.for_data_dir(config.data_dir).path(artifact.sha256)


def test_reprocess_creates_a_missing_preview_once(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    sermon_id, _ = processed(config, engine)
    with sessions.begin() as s:
        service_id = SermonRepository(s).get(sermon_id).service_id
        [preview] = previews(s, service_id)
        s.delete(preview)  # as if processed before previews existed
    process_service(config, sessions, service_id, processors(), reprocess=True)
    process_service(config, sessions, service_id, processors(), reprocess=True)
    with sessions() as s:
        [preview] = previews(s, service_id)
        assert abs((preview.duration_ms or 0) - 12_000) < 200


def test_render_edited_boundaries(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    sermon_id, first = processed(config, engine)
    edited = PROPOSED.model_copy(update={"sermon_start_ms": 2_000})
    with sessions.begin() as s:
        repo = SermonRepository(s)
        old = repo.revision(first)
        revision_id = repo.add_revision(sermon_id, author="user:1", boundaries=edited,
                                        metadata=repo.metadata(old),
                                        transcript_markdown=old.transcript_markdown).id  # fmt: skip
    procs = processors()
    render_revision(config, sessions, revision_id, procs)
    assert procs.formatter.calls == 1  # type: ignore[union-attr]
    assert procs.transcriber.calls == 1  # type: ignore[attr-defined]
    with sessions() as s:
        revision = SermonRepository(s).current_revision(sermon_id)
        assert revision is not None and revision.id != revision_id
        audio = ArtifactRepository(s).get(revision.sermon_audio_artifact_id or 0)
        assert audio.meta["boundaries"] == [1_000, 2_000, 8_000, 11_000]
        assert abs((audio.duration_ms or 0) - 6_000) < 200
        count = len(ArtifactRepository(s).for_service(audio.service_id))
    render_revision(config, sessions, revision_id, procs)  # already rendered: no-op
    assert procs.transcriber.calls == 1  # type: ignore[attr-defined]
    with sessions() as s:
        assert len(ArtifactRepository(s).for_service(audio.service_id)) == count
        assert len(previews(s, audio.service_id)) == 1  # the preview is never re-cut
        cuts = [
            a
            for a in ArtifactRepository(s).for_service(audio.service_id)
            if a.meta.get("preview") and a.meta["boundaries"] == audio.meta["boundaries"]
        ]
        assert sorted(str(a.meta["name"]) for a in cuts) == ["sermon_preview", "service_preview"]
        sermon_cut = next(a for a in cuts if a.meta["name"] == "sermon_preview")
        assert abs((sermon_cut.duration_ms or 0) - 6_000) < 200


def test_render_adds_missing_cut_previews(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    sermon_id, revision_id = processed(config, engine)
    with sessions.begin() as s:
        service_id = SermonRepository(s).get(sermon_id).service_id
        for artifact in cut_previews(s, service_id).values():
            s.delete(artifact)  # as if rendered before cut previews existed
    render_revision(config, sessions, revision_id, processors())
    with sessions() as s:
        assert sorted(cut_previews(s, service_id)) == ["sermon_preview", "service_preview"]


def test_publication_never_uses_cut_previews(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    sermon_id, revision_id = processed(config, engine)
    with sessions() as s:
        sermons = SermonRepository(s)
        service_id = sermons.get(sermon_id).service_id
        boundaries = sermons.boundaries(sermons.revision(revision_id))
        names = set(_renditions_for(s, service_id, boundaries))
    assert names == {"sermon", "service_hq", "service_phone"}


def test_render_preserves_edited_metadata_and_replaces_unedited_transcript(
    config: GeorgConfig, engine: Engine
) -> None:
    sessions = session_factory(engine)
    sermon_id, first = processed(config, engine)
    edited = PROPOSED.model_copy(update={"sermon_end_ms": 9_000})
    with sessions.begin() as s:
        repo = SermonRepository(s)
        old = repo.revision(first)
        metadata = repo.metadata(old).model_copy(update={"title": "Reviewed title"})
        revision_id = repo.add_revision(
            sermon_id,
            author="user:1",
            boundaries=edited,
            metadata=metadata,
            transcript_markdown=old.transcript_markdown,
            field_origins={"metadata": "user"},
        ).id
    procs = processors()
    render_revision(config, sessions, revision_id, procs)
    assert procs.formatter.calls == 1  # type: ignore[union-attr]
    assert procs.transcriber.calls == 1  # type: ignore[attr-defined]
    with sessions() as s:
        current = SermonRepository(s).current_revision(sermon_id)
        assert current is not None and current.id != revision_id
        assert SermonRepository(s).metadata(current).title == "Reviewed title"
        assert current.field_origins["metadata"] == "user"
        assert current.transcript_markdown != old.transcript_markdown


def test_render_preserves_edited_transcript(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    sermon_id, first = processed(config, engine)
    edited = PROPOSED.model_copy(update={"sermon_end_ms": 9_000})
    transcript = "# My wording\n\nA reviewer-edited sentence.\n"
    with sessions.begin() as s:
        repo = SermonRepository(s)
        old = repo.revision(first)
        revision_id = repo.add_revision(
            sermon_id,
            author="user:1",
            boundaries=edited,
            metadata=repo.metadata(old),
            transcript_markdown=transcript,
            field_origins={"transcript": "user"},
        ).id
    procs = processors()
    render_revision(config, sessions, revision_id, procs)
    assert procs.formatter.calls == 0  # type: ignore[union-attr]
    assert procs.transcriber.calls == 0  # type: ignore[attr-defined]
    assert procs.metadata.calls == 0  # type: ignore[attr-defined]
    with sessions() as s:
        current = SermonRepository(s).current_revision(sermon_id)
        assert current is not None and current.id == revision_id
        assert current.transcript_markdown == transcript
        assert current.field_origins["transcript"] == "user"
        assert current.sermon_audio_artifact_id is not None


def test_render_service_only_boundary_change_keeps_revision(
    config: GeorgConfig, engine: Engine
) -> None:
    sessions = session_factory(engine)
    sermon_id, first = processed(config, engine)
    edited = PROPOSED.model_copy(update={"service_end_ms": 10_000})
    with sessions.begin() as s:
        repo = SermonRepository(s)
        old = repo.revision(first)
        revision_id = repo.add_revision(
            sermon_id,
            author="user:1",
            boundaries=edited,
            metadata=repo.metadata(old),
            transcript_markdown=old.transcript_markdown,
        ).id
    procs = processors()
    render_revision(config, sessions, revision_id, procs)
    assert procs.formatter.calls == 0  # type: ignore[union-attr]
    assert procs.transcriber.calls == 0  # type: ignore[attr-defined]
    assert procs.metadata.calls == 0  # type: ignore[attr-defined]
    with sessions() as s:
        current = SermonRepository(s).current_revision(sermon_id)
        assert current is not None and current.id == revision_id
        assert current.sermon_audio_artifact_id is not None


def test_publish_requires_approval_then_publishes_once(config: GeorgConfig, engine: Engine) -> None:
    sessions = session_factory(engine)
    sermon_id, revision_id = processed(config, engine)
    good = FakeOutput("phone", {"service_phone"})
    with pytest.raises(ModuleError, match="not approved"):
        publish_sermon(config, sessions, sermon_id, outputs=[good])
    with sessions.begin() as s:
        user = UserRepository(s).add("rev", "x", "admin")
        SermonRepository(s).approve(revision_id, approved_by=user.id)

    bad = FakeOutput("website", {"sermon"}, fail=ModuleError("down", retryable=True))
    with pytest.raises(ModuleError, match="website: down") as err:
        publish_sermon(config, sessions, sermon_id, outputs=[good, bad])
    assert err.value.retryable
    [request] = good.requests
    assert request.publication_key == "georg:test:sermon-1:phone"
    assert request.transcript_markdown is not None and request.service_date.day == 1

    bad.fail = None
    statuses = publish_sermon(config, sessions, sermon_id, outputs=[good, bad])
    assert statuses == {"phone": "published", "website": "published"}
    assert len(good.requests) == 1  # not re-sent
    with sessions() as s:
        pubs = {p.target_name: p for p in PublicationRepository(s).for_sermon(sermon_id)}
        assert pubs["website"].status == "published"
        assert pubs["website"].external_id == "id-2"


def test_publish_waits_for_missing_renditions(config: GeorgConfig, engine: Engine) -> None:
    config = config.model_copy(
        update={"review": config.review.model_copy(update={"require_approval": False})}
    )
    sermon_id, _ = processed(config, engine)
    with pytest.raises(ModuleError, match="not rendered yet") as err:
        publish_sermon(config, session_factory(engine), sermon_id,
                       outputs=[FakeOutput("x", {"nope"})])  # fmt: skip
    assert err.value.retryable


def test_publish_builds_outputs_in_instance_timezone(
    config: GeorgConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config.model_copy(
        update={
            "timezone": "America/Chicago",
            "outputs": (OutputConfig(name="website", type="wordpress"),),
        }
    )
    sermon_id, _ = processed(config, engine)
    built: list[tuple[str, str]] = []

    def fake_build(c: OutputConfig, *, timezone: str = "UTC") -> FakeOutput:
        built.append((c.name, timezone))
        return FakeOutput(c.name, {"sermon"})

    monkeypatch.setattr("georg.pipeline.service.build_output", fake_build)
    with pytest.raises(ModuleError, match="not approved"):
        publish_sermon(config, session_factory(engine), sermon_id)
    assert built == [("website", "America/Chicago")]


def test_worker_queues_notice_when_reviewers_configured(
    config: GeorgConfig, engine: Engine
) -> None:
    review = config.review.model_copy(update={"notify": ("a@x.org",)})
    config = config.model_copy(update={"review": review})
    queue = JobQueue(engine)
    discover(config, session_factory(engine), queue)
    worker = Worker(config, engine, queue=queue, processors=processors())
    sent: list[dict[str, object]] = []
    worker.handlers["notify"] = sent.append
    assert worker.run_once() and worker.run_once()
    assert sent == [{"service_id": 1}]


def test_worker_reads_the_bulletin_for_metadata(
    config: GeorgConfig, engine: Engine, tmp_path: Path
) -> None:
    name = "2026-11-01_All_Saints__Day_-_DS1-Eric.docx"
    doc = make_docx(tmp_path / "drive" / "SPX 11.01.26" / name,
                    [["Feast of All Saints"], ["November 1, 2026"]])  # fmt: skip
    old = time.time() - 3600
    os.utime(doc, (old, old))
    sessions = session_factory(engine)
    queue = JobQueue(engine)
    [service_id] = discover(config, sessions, queue)
    metadata = RulesMetadataGenerator({"title": "Sermon for {occasion}"})
    procs = dataclasses.replace(processors(), metadata=metadata)
    assert Worker(config, engine, queue=queue, processors=procs, worker_id="w").run_once()
    with sessions() as s:
        [sermon] = SermonRepository(s).for_service(service_id)
        revision = SermonRepository(s).current_revision(sermon.id)
        assert revision is not None
        meta = SermonRepository.metadata(revision)
        assert meta.occasion == "Feast of All Saints"
        assert meta.title == "Sermon for Feast of All Saints"
        assert revision.field_origins["occasion"] == f"rules-3/document text: {name}"
        assert "boundaries" in revision.field_origins
