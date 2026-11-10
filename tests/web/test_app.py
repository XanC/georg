# SPDX-License-Identifier: AGPL-3.0-only
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from georg.core.config import GeorgConfig
from georg.core.errors import ConfigError
from georg.core.types import SermonMetadata, ServiceBoundaries, SourceItem, SourcePart
from georg.db.engine import session_factory
from georg.db.repositories import (
    ArtifactRepository,
    SermonRepository,
    ServiceRepository,
    UserRepository,
)
from georg.jobs.queue import JobQueue
from georg.storage.local import LocalStore
from georg.web import create_app
from georg.web.auth import hash_password

PASSWORD = "correct horse battery"
BOUNDS = ServiceBoundaries(
    service_start_ms=60_000,
    sermon_start_ms=1_200_500,
    sermon_end_ms=2_400_000,
    service_end_ms=3_600_000,
)
META = SermonMetadata(title="Saints", preacher="Eric", scripture=("Matthew 5:1-12",))
AUDIO = bytes(range(256)) * 40
REPORT = {
    "proposal": {"confidence": 0.82, "explanation": "Sermon begins after the gospel reading."},
    "boundaries": {"service_start_ms": 60_000},
    "words_preserved": False,
    "discrepancies": ["dropped 'amen' near 0:31:02"],
}


@dataclass
class Env:
    client: TestClient
    queue: JobQueue
    engine: Engine
    sermon_id: int
    revision_id: int
    audio_id: int
    service_id: int


def _key(b: ServiceBoundaries) -> list[int]:
    return [b.service_start_ms, b.sermon_start_ms, b.sermon_end_ms, b.service_end_ms]


def _config(tmp_path: Path) -> GeorgConfig:
    return GeorgConfig.model_validate({
        "instance": "test",
        "data_dir": tmp_path / "data",
        "inputs": [{"name": "drive", "type": "local_folder", "options": {"root": tmp_path}}],
        "processors": {k: {"type": "unused"} for k in
                       ("classifier", "detector", "transcriber", "formatter", "metadata")},
    })  # fmt: skip


@pytest.fixture
def env(engine: Engine, tmp_path: Path) -> Iterator[Env]:
    config = _config(tmp_path)
    store = LocalStore.for_data_dir(config.data_dir)
    (tmp_path / "service_hq.mp3").write_bytes(AUDIO)
    (tmp_path / "report.json").write_text(json.dumps(REPORT))
    audio_ref = store.put(tmp_path / "service_hq.mp3", mime="audio/mpeg")
    report_ref = store.put(tmp_path / "report.json", mime="application/json")
    item = SourceItem(
        source_type="local_folder",
        source_ref="SPX 11.01.26",
        parts=(SourcePart(ref="1.wav", name="1.wav", size=1, checksum="x"),),
        title="SPX 11.01.26",
        service_date=date(2026, 11, 1),
    )
    with session_factory(engine).begin() as s:
        UserRepository(s).add("rev", hash_password(PASSWORD), "reviewer")
        UserRepository(s).add("root", hash_password(PASSWORD), "admin")
        service, _ = ServiceRepository(s).add_discovered(item)
        artifacts = ArtifactRepository(s)
        audio = artifacts.add(
            service.id,
            "rendition",
            audio_ref,
            storage_key=audio_ref.sha256,
            meta={"name": "service_hq", "boundaries": _key(BOUNDS)},
        )
        artifacts.add(
            service.id,
            "boundary_proposal",
            report_ref,
            storage_key=report_ref.sha256,
            meta={"name": "report"},
        )
        sermons = SermonRepository(s)
        sermon = sermons.create(service.id)
        revision = sermons.add_revision(
            sermon.id,
            author="system",
            boundaries=BOUNDS,
            metadata=META,
            transcript_markdown="# Saints\n\nBlessed are the poor in spirit.",
            field_origins={"boundaries": "detector-1"},
        )
        ids = (sermon.id, revision.id, audio.id, service.id)
    queue = JobQueue(engine)
    app = create_app(config, engine, secret_key="test-secret", store=store, queue=queue)
    with TestClient(app) as client:
        yield Env(client, queue, engine, *ids)


def _csrf(client: TestClient, path: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match
    return match.group(1)


def _login(env: Env, username: str = "rev") -> None:
    token = _csrf(env.client, "/login")
    response = env.client.post(
        "/login",
        data={"username": username, "password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _form(env: Env, **changes: str) -> dict[str, str]:
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    form = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html))
    for name in ("scripture", "summary", "transcript"):
        match = re.search(rf'<textarea[^>]*name="{name}"[^>]*>(.*?)</textarea>', html, re.S)
        assert match
        form[name] = match.group(1)
    form.pop("revision_id", None)
    form.pop("note", None)
    return {**form, **changes}


def _jobs(env: Env, kind: str) -> list[dict[str, object]]:
    return [j.payload for j in env.queue.list() if j.kind == kind]


def test_healthz_needs_no_login(env: Env) -> None:
    response = env.client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


@pytest.mark.parametrize("path", ["/", "/jobs", "/sermons/1", "/artifacts/1"])
def test_pages_require_login(env: Env, path: str) -> None:
    response = env.client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_wrong_password_is_rejected(env: Env) -> None:
    token = _csrf(env.client, "/login")
    response = env.client.post(
        "/login", data={"username": "rev", "password": "nope", "csrf_token": token}
    )
    assert response.status_code == 401
    assert env.client.get("/", follow_redirects=False).status_code == 303


def test_login_list_and_logout(env: Env) -> None:
    _login(env)
    page = env.client.get("/")
    assert page.status_code == 200
    assert "SPX 11.01.26" in page.text
    assert "needs review" in page.text
    token = _csrf(env.client, "/")
    assert env.client.post("/logout", data={"csrf_token": token}).status_code == 200
    assert env.client.get("/", follow_redirects=False).status_code == 303


def test_create_app_refuses_without_secret(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEORG_SECRET_KEY", raising=False)
    with pytest.raises(ConfigError):
        create_app(_config(tmp_path), engine)


def test_post_without_csrf_token_is_rejected(env: Env) -> None:
    _login(env)
    form = _form(env, title="Hijacked")
    for bad in ("", "forged"):
        form["csrf_token"] = bad
        response = env.client.post(f"/sermons/{env.sermon_id}/revisions", data=form)
        assert response.status_code == 403
    assert env.client.post("/jobs/discover").status_code == 403
    with session_factory(env.engine)() as s:
        assert len(SermonRepository(s).revisions(env.sermon_id)) == 1
    assert env.queue.list() == []


def test_login_post_requires_csrf(env: Env) -> None:
    response = env.client.post("/login", data={"username": "rev", "password": PASSWORD})
    assert response.status_code == 403


def test_review_page_shows_report_and_play_controls(env: Env) -> None:
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert 'value="0:20:00"' in html
    assert "82%" in html
    assert "after the gospel reading" in html
    assert "dropped &#39;amen&#39;" in html
    assert f'src="/artifacts/{env.audio_id}"' in html
    assert 'id="seek-audio" data-origin-ms="60000"' in html
    assert "Blessed are the poor" in html


def test_save_metadata_creates_revision_without_render(env: Env) -> None:
    _login(env)
    response = env.client.post(
        f"/sermons/{env.sermon_id}/revisions",
        data=_form(env, title="All Saints", transcript="# Saints\n\nBlessed are the meek."),
    )
    assert response.status_code == 200
    assert "Saved revision 2." in response.text
    with session_factory(env.engine)() as s:
        sermons = SermonRepository(s)
        revision = sermons.current_revision(env.sermon_id)
        assert revision is not None
        assert revision.number == 2
        assert revision.author.startswith("user:")
        assert sermons.metadata(revision).title == "All Saints"
        assert sermons.metadata(revision).preacher == "Eric"
        assert sermons.boundaries(revision) == BOUNDS
        assert revision.transcript_markdown == "# Saints\n\nBlessed are the meek."
        assert revision.field_origins == {
            "boundaries": "detector-1",
            "title": "user",
            "transcript": "user",
        }
    assert _jobs(env, "render") == []


def test_unchanged_form_saves_nothing(env: Env) -> None:
    _login(env)
    response = env.client.post(f"/sermons/{env.sermon_id}/revisions", data=_form(env))
    assert "No changes to save." in response.text
    with session_factory(env.engine)() as s:
        assert len(SermonRepository(s).revisions(env.sermon_id)) == 1
    assert env.queue.list() == []


def test_boundary_change_enqueues_render(env: Env) -> None:
    _login(env)
    response = env.client.post(
        f"/sermons/{env.sermon_id}/revisions", data=_form(env, sermon_end="0:41:30")
    )
    assert "being re-rendered" in response.text
    with session_factory(env.engine)() as s:
        sermons = SermonRepository(s)
        revision = sermons.current_revision(env.sermon_id)
        assert revision is not None
        assert sermons.boundaries(revision).sermon_end_ms == 2_490_000
        assert sermons.boundaries(revision).sermon_start_ms == 1_200_500
        assert revision.field_origins["boundaries"] == "user"
        assert revision.sermon_audio_artifact_id is None
        new_id = revision.id
    assert _jobs(env, "render") == [{"revision_id": new_id}]


def test_invalid_boundaries_are_not_saved(env: Env) -> None:
    _login(env)
    response = env.client.post(
        f"/sermons/{env.sermon_id}/revisions", data=_form(env, sermon_end="0:10:00")
    )
    assert response.status_code == 400
    assert "Times must be in order" in response.text
    assert env.queue.list() == []


def test_stale_form_is_not_saved(env: Env) -> None:
    _login(env)
    stale = _form(env, title="First")
    env.client.post(f"/sermons/{env.sermon_id}/revisions", data=_form(env, title="Second"))
    response = env.client.post(f"/sermons/{env.sermon_id}/revisions", data=stale)
    assert response.status_code == 400
    assert "was saved while you were editing" in response.text


def test_restore_creates_revision_and_renders_moved_boundaries(env: Env) -> None:
    _login(env)
    env.client.post(f"/sermons/{env.sermon_id}/revisions", data=_form(env, sermon_end="0:41:30"))
    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    response = env.client.post(
        f"/sermons/{env.sermon_id}/revisions/{env.revision_id}/restore",
        data={"csrf_token": token},
    )
    assert "Restored revision 1 as revision 3." in response.text
    with session_factory(env.engine)() as s:
        revision = SermonRepository(s).current_revision(env.sermon_id)
        assert revision is not None
        assert SermonRepository(s).boundaries(revision) == BOUNDS
        restored_id = revision.id
    assert _jobs(env, "render")[-1] == {"revision_id": restored_id}


def test_approve_enqueues_publish(env: Env) -> None:
    _login(env)
    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    response = env.client.post(
        f"/sermons/{env.sermon_id}/approve",
        data={"csrf_token": token, "revision_id": str(env.revision_id), "note": "ok"},
    )
    assert "publishing has been queued" in response.text
    with session_factory(env.engine)() as s:
        approval = SermonRepository(s).current_approval(env.sermon_id)
        assert approval is not None
        assert approval.revision_id == env.revision_id
    assert _jobs(env, "publish") == [{"sermon_id": env.sermon_id}]
    assert "approved (revision 1)" in env.client.get("/").text


def test_approving_a_stale_revision_is_refused(env: Env) -> None:
    _login(env)
    env.client.post(f"/sermons/{env.sermon_id}/revisions", data=_form(env, title="Newer"))
    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    response = env.client.post(
        f"/sermons/{env.sermon_id}/approve",
        data={"csrf_token": token, "revision_id": str(env.revision_id)},
    )
    assert "A newer revision exists" in response.text
    assert _jobs(env, "publish") == []


def test_reprocess_button_is_admin_only(env: Env) -> None:
    _login(env)
    assert "reprocess" not in env.client.get(f"/sermons/{env.sermon_id}").text
    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    response = env.client.post(f"/sermons/{env.sermon_id}/reprocess", data={"csrf_token": token})
    assert response.status_code == 403
    assert _jobs(env, "process") == []

    _login(env, username="root")
    assert "Reprocess this service" in env.client.get(f"/sermons/{env.sermon_id}").text
    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    response = env.client.post(f"/sermons/{env.sermon_id}/reprocess", data={"csrf_token": token})
    assert "Reprocessing queued" in response.text
    assert _jobs(env, "process") == [{"service_id": env.service_id, "reprocess": True}]


def test_artifact_supports_range_requests(env: Env) -> None:
    _login(env)
    url = f"/artifacts/{env.audio_id}"
    full = env.client.get(url)
    assert full.status_code == 200
    assert full.content == AUDIO
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"] == "audio/mpeg"
    part = env.client.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == AUDIO[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(AUDIO)}"
    tail = env.client.get(url, headers={"Range": "bytes=-10"})
    assert tail.content == AUDIO[-10:]
    assert env.client.get(url, headers={"Range": "bytes=99999-"}).status_code == 416
    assert env.client.get("/artifacts/999").status_code == 404


def test_jobs_page_and_discover(env: Env) -> None:
    _login(env)
    job_id = env.queue.enqueue("process_service", {"service_id": 1}, max_attempts=1)
    leased = env.queue.lease("w")
    assert leased is not None
    env.queue.fail(job_id, "w", "ffmpeg exploded")
    token = _csrf(env.client, "/jobs")
    assert "ffmpeg exploded" in env.client.get("/jobs").text
    response = env.client.post("/jobs/discover", data={"csrf_token": token})
    assert "Checking for new recordings" in response.text
    env.client.post("/jobs/discover", data={"csrf_token": token})
    assert _jobs(env, "discover") == [{}]
    assert "discover" in env.client.get("/jobs/table").text


def test_htmx_is_served_locally(env: Env) -> None:
    response = env.client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text
