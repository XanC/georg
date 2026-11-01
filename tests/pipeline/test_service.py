# SPDX-License-Identifier: AGPL-3.0-only
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import Engine
from test_local_run import PROPOSED, processors, tone

from georg.core.config import GeorgConfig
from georg.db.engine import session_factory
from georg.db.repositories import ArtifactRepository, SermonRepository, ServiceRepository
from georg.jobs.queue import JobQueue
from georg.pipeline import Worker, discover, process_service


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
        assert kinds == ["boundary_proposal", "rendition", "rendition", "sermon_audio",
                         "transcript_formatted"]  # fmt: skip
        assert revision.sermon_audio_artifact_id is not None
        service = ServiceRepository(s).get(service_id)
        assert service.status_summary == "awaiting review"
        assert service.duration_ms is not None and service.duration_ms > 11_000

    # A retry after the proposal was saved changes nothing.
    assert process_service(config, sessions, service_id, processors()) is None


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
