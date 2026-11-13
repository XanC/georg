# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from georg.core.config import GeorgConfig
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import (
    ExternalRef,
    SermonMetadata,
    ServiceBoundaries,
    SourceItem,
    SourcePart,
)
from georg.db.engine import session_factory
from georg.db.repositories import (
    ArtifactRepository,
    PublicationRepository,
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
PREVIEW_AUDIO = bytes(range(256)) * 4096
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


def _config(tmp_path: Path, review: dict[str, object] | None = None) -> GeorgConfig:
    review = dict(review or {})
    outputs = review.pop("outputs", ())
    return GeorgConfig.model_validate({
        "review": review,
        "outputs": outputs,
        "instance": "test",
        "data_dir": tmp_path / "data",
        "inputs": [{"name": "drive", "type": "local_folder", "options": {"root": tmp_path}}],
        "processors": {k: {"type": "unused"} for k in
                       ("classifier", "detector", "transcriber", "formatter", "metadata")},
    })  # fmt: skip


@pytest.fixture
def env(engine: Engine, tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Env]:
    config = _config(tmp_path, getattr(request, "param", None))
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
        if match:  # absent when hidden
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
    assert "Needs review" in page.text
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
    assert "Blessed are the poor" in html


def _add_preview(env: Env, tmp_path: Path) -> int:
    path = tmp_path / "preview.mp3"
    path.write_bytes(PREVIEW_AUDIO)
    ref = LocalStore.for_data_dir(tmp_path / "data").put(path, mime="audio/mpeg")
    with session_factory(env.engine).begin() as s:
        meta = {"name": "preview", "span": "recording"}
        artifact = ArtifactRepository(s).add(
            env.service_id, "rendition", ref, storage_key=ref.sha256, meta=meta
        )
        return artifact.id


def test_review_page_without_preview_falls_back_to_service_hq(env: Env) -> None:
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert "has no whole-recording preview yet" in html
    assert "Reprocess the service" in html
    assert 'id="seek-audio" data-origin-ms="60000"' in html
    assert (
        '<output id="player-position" for="seek-audio" class="position">0:01:00.0</output>' in html
    )
    assert html.count('class="play-from"') == 4
    assert "set-from" not in html


def test_review_page_with_preview_uses_recording_time(env: Env, tmp_path: Path) -> None:
    preview_id = _add_preview(env, tmp_path)
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert "no whole-recording preview" not in html
    main = f'src="/artifacts/{preview_id}" id="seek-audio" data-origin-ms="0"'
    assert main in html
    assert html.count('id="seek-audio"') == 1
    assert html.index(main) < html.index("Rendered audio (cut at the boundaries)")
    assert html.index("cut at the boundaries") < html.index(f'src="/artifacts/{env.audio_id}"')
    assert html.count(">preview<") == 0  # not listed again among the cut renditions
    assert (
        '<output id="player-position" for="seek-audio" class="position">0:00:00.0</output>' in html
    )
    for name in ("service_start", "sermon_start", "sermon_end", "service_end"):
        assert f'class="play-from" data-field="f-{name}"' in html


def _add_cut_preview(env: Env, tmp_path: Path, name: str, span: str) -> int:
    path = tmp_path / f"{name}.mp3"
    path.write_bytes(AUDIO + name.encode())
    ref = LocalStore.for_data_dir(tmp_path / "data").put(path, mime="audio/mpeg")
    with session_factory(env.engine).begin() as s:
        meta = {"name": name, "span": span, "preview": True, "boundaries": _key(BOUNDS)}
        return (
            ArtifactRepository(s)
            .add(env.service_id, "rendition", ref, storage_key=ref.sha256, meta=meta)
            .id
        )


def test_review_page_shows_cut_previews_and_downloads(env: Env, tmp_path: Path) -> None:
    _add_preview(env, tmp_path)
    service_id = _add_cut_preview(env, tmp_path, "service_preview", "service")
    sermon_id = _add_cut_preview(env, tmp_path, "sermon_preview", "sermon")
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    sermon_label = "Sermon (as cut, 0:20:00.5\N{EN DASH}0:40:00.0)"
    service_label = "Service (as cut, 0:01:00.0\N{EN DASH}1:00:00.0)"
    assert html.index(sermon_label) < html.index(service_label)
    for artifact_id in (sermon_id, service_id):
        assert f'<audio controls preload="metadata" src="/artifacts/{artifact_id}"' in html
    assert f'<a href="/artifacts/{env.audio_id}" download>service_hq</a>' in html
    assert f'src="/artifacts/{env.audio_id}"' not in html  # no full-quality player


def test_review_page_without_cut_previews_keeps_full_quality_players(env: Env) -> None:
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert f'src="/artifacts/{env.audio_id}"' in html
    assert "(as cut," not in html
    assert "Full quality:" not in html


@pytest.mark.parametrize("env", [{"hidden_fields": ["series", "summary"]}], indirect=True)
def test_hidden_fields_are_left_off_and_kept(env: Env) -> None:
    with session_factory(env.engine).begin() as s:
        sermons = SermonRepository(s)
        sermons.add_revision(
            env.sermon_id,
            author="system",
            boundaries=BOUNDS,
            metadata=META.model_copy(update={"series": "Beatitudes", "summary": "Blessed."}),
            transcript_markdown="# Saints",
        )
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert 'name="series"' not in html
    assert 'name="summary"' not in html
    assert 'name="occasion"' in html
    response = env.client.post(
        f"/sermons/{env.sermon_id}/revisions", data=_form(env, title="All Saints")
    )
    assert "Saved revision 3." in response.text
    with session_factory(env.engine)() as s:
        sermons = SermonRepository(s)
        revision = sermons.current_revision(env.sermon_id)
        assert revision is not None
        metadata = sermons.metadata(revision)
    assert (metadata.title, metadata.series, metadata.summary) == (
        "All Saints", "Beatitudes", "Blessed."
    )  # fmt: skip


def test_preview_supports_range_requests(env: Env, tmp_path: Path) -> None:
    preview_id = _add_preview(env, tmp_path)
    _login(env)
    part = env.client.get(f"/artifacts/{preview_id}", headers={"Range": "bytes=500000-500999"})
    assert part.status_code == 206
    assert part.content == PREVIEW_AUDIO[500_000:501_000]
    assert part.headers["content-range"] == f"bytes 500000-500999/{len(PREVIEW_AUDIO)}"
    assert part.headers["content-length"] == "1000"


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
    assert "Approved revision 1." in response.text
    assert "queued" not in response.text
    assert 'hx-get="/sermons/1/publish-status" hx-trigger="every 3s"' in response.text
    with session_factory(env.engine)() as s:
        approval = SermonRepository(s).current_approval(env.sermon_id)
        assert approval is not None
        assert approval.revision_id == env.revision_id
    assert _jobs(env, "publish") == [{"sermon_id": env.sermon_id}]
    assert f'data-status="{env.service_id}">Approved<' in env.client.get("/").text


def _publication(env: Env, target: str, *, error: str | None = None) -> None:
    with session_factory(env.engine).begin() as s:
        pubs = PublicationRepository(s)
        pub = pubs.prepare(env.sermon_id, target, env.revision_id, f"key-{target}")
        pubs.mark_publishing(pub.id)
        if error:
            pubs.mark_failed(pub.id, error)
        else:
            pubs.mark_published(pub.id, ExternalRef(target=target, external_id="1"))


def _approve(env: Env) -> None:
    with session_factory(env.engine).begin() as s:
        SermonRepository(s).approve(env.revision_id, approved_by=1)


def test_publish_status_shows_publishing_while_the_job_is_active(env: Env) -> None:
    _login(env)
    _approve(env)
    _publication(env, "website")
    _publication(env, "podcast", error="HTTP 500")
    job_id = env.queue.enqueue("publish", {"sermon_id": env.sermon_id})
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert 'hx-trigger="every 3s"' in html
    # website already holds this revision, so the job leaves it alone.
    assert 'data-output="website">Published<' in html
    assert 'data-output="podcast">Publishing…<' in html

    leased = env.queue.lease("w")
    assert leased is not None and leased.id == job_id
    fragment = env.client.get(f"/sermons/{env.sermon_id}/publish-status").text
    assert 'data-output="podcast">Publishing…<' in fragment

    env.queue.complete(job_id, "w")
    fragment = env.client.get(f"/sermons/{env.sermon_id}/publish-status").text
    assert fragment.startswith('<div id="publish-status"')
    assert "hx-trigger" not in fragment
    assert 'data-output="website">Published<' in fragment
    assert 'data-output="podcast">Failed: HTTP 500<' in fragment
    assert "Publish again" in fragment


def test_publish_again_does_not_flash_a_contradicting_message(env: Env) -> None:
    _login(env)
    _approve(env)
    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    response = env.client.post(f"/sermons/{env.sermon_id}/publish", data={"csrf_token": token})
    assert "queued" not in response.text
    assert "Publishing…" in response.text
    assert _jobs(env, "publish") == [{"sermon_id": env.sermon_id}]


OUTPUTS = {"outputs": [{"name": "website", "type": "unused"}, {"name": "phone", "type": "unused"}]}


def _status(env: Env) -> str:
    match = re.search(rf'data-status="{env.service_id}">([^<]*)<', env.client.get("/").text)
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize("env", [OUTPUTS], indirect=True)
def test_services_list_status_follows_review_and_publication(env: Env) -> None:
    _login(env)
    html = env.client.get("/").text
    assert '<th scope="col">Sermon</th>' not in html
    assert "awaiting review" not in html
    assert _status(env) == "Needs review"

    _approve(env)
    assert _status(env) == "Approved"
    job_id = env.queue.enqueue("publish", {"sermon_id": env.sermon_id})
    assert _status(env) == "Publishing"
    assert env.queue.lease("w") is not None
    env.queue.complete(job_id, "w")

    _publication(env, "website")
    with session_factory(env.engine).begin() as s:
        PublicationRepository(s).prepare(env.sermon_id, "phone", env.revision_id, "key-phone")
    assert _status(env) == "Publishing"
    _publication(env, "phone", error="SFTP refused")
    assert _status(env) == "Publish failed (phone)"
    _publication(env, "phone")
    assert _status(env) == "Published"
    review = env.client.get(f"/sermons/{env.sermon_id}").text
    assert '<span class="badge">Published</span>' in review

    env.client.post(f"/sermons/{env.sermon_id}/revisions", data=_form(env, title="Newer"))
    assert _status(env) == "Edited since approval"


def test_service_without_a_revision_keeps_its_processing_status(env: Env) -> None:
    item = SourceItem(
        source_type="local_folder",
        source_ref="SPX 11.15.26",
        parts=(SourcePart(ref="2.wav", name="2.wav", size=1, checksum="y"),),
        title="SPX 11.15.26",
        service_date=date(2026, 11, 15),
    )
    with session_factory(env.engine).begin() as s:
        service, _ = ServiceRepository(s).add_discovered(item)
        service.status_summary = "transcribing"
        service_id = service.id
    _login(env)
    assert f'data-status="{service_id}">transcribing<' in env.client.get("/").text
    assert "Status: transcribing." in env.client.get(f"/services/{service_id}").text


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


def test_review_page_plays_one_player_at_a_time(env: Env) -> None:
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert '<script src="http://testserver/static/review.js" defer></script>' in html
    script = env.client.get("/static/review.js")
    assert script.status_code == 200
    assert 'document.addEventListener("play", function (event) {' in script.text
    assert 'querySelectorAll("audio, video")' in script.text
    assert "}, true);" in script.text


def test_review_page_says_where_derived_fields_came_from(env: Env) -> None:
    with session_factory(env.engine).begin() as s:
        SermonRepository(s).add_revision(
            env.sermon_id,
            author="system",
            boundaries=BOUNDS,
            metadata=META.model_copy(update={"occasion": "All Saints' Day"}),
            transcript_markdown="# Saints",
            field_origins={
                "scripture": "scripture-1/llm: local model picked Gospel",
                "occasion": "rules-3/document text: bulletin.docx",
            },
        )
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert "Scripture from scripture-1/llm: local model picked Gospel" in html
    assert "Occasion from rules-3/document text: bulletin.docx" in html


def _other_sermon(env: Env, ref: str, when: date, preacher: str) -> None:
    item = SourceItem(
        source_type="local_folder",
        source_ref=ref,
        parts=(SourcePart(ref=f"{ref}.wav", name="1.wav"),),
        service_date=when,
    )
    with session_factory(env.engine).begin() as s:
        service, _ = ServiceRepository(s).add_discovered(item)
        sermons = SermonRepository(s)
        sermon = sermons.create(service.id)
        sermons.add_revision(
            sermon.id,
            author="system",
            boundaries=BOUNDS,
            metadata=SermonMetadata(preacher=preacher),
        )


def test_review_page_without_preacher_suggestions(env: Env) -> None:
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert 'name="preacher" value="Eric">' in html
    assert "<datalist" not in html


@pytest.mark.parametrize(
    "env", [{"preachers": ["The Rev. Seed", "Pastor Jane", "Eric"]}], indirect=True
)
def test_review_page_suggests_preachers(env: Env) -> None:
    _other_sermon(env, "old", date(2025, 1, 5), "Pastor Old")
    _other_sermon(env, "jane", date(2026, 10, 25), "Pastor Jane")
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert 'name="preacher" value="Eric" list="preacher-suggestions" autocomplete="off">' in html
    assert (
        '<datalist id="preacher-suggestions"><option value="Pastor Jane">'
        '<option value="Pastor Old"><option value="The Rev. Seed"><option value="Eric">'
        "</datalist>"
    ) in html


@pytest.mark.parametrize(
    "env", [{"hidden_fields": ["preacher"], "preachers": ["The Rev. Seed"]}], indirect=True
)
def test_hidden_preacher_renders_nothing(env: Env) -> None:
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert 'name="preacher"' not in html and "<datalist" not in html
    response = env.client.post(
        f"/sermons/{env.sermon_id}/revisions", data=_form(env, title="All Saints")
    )
    assert "Saved revision" in response.text
    with session_factory(env.engine)() as s:
        sermons = SermonRepository(s)
        revision = sermons.current_revision(env.sermon_id)
        assert revision is not None and sermons.metadata(revision).preacher == "Eric"


WORDPRESS = {"outputs": [{"name": "website", "type": "wordpress", "options": {}}]}


class _FakeSite:
    def __init__(self, posts: list[dict[str, str]] | None) -> None:
        self.posts = posts
        self.calls: list[tuple[date, date]] = []

    def sermons_between(self, first: date, last: date) -> list[dict[str, str]]:
        self.calls.append((first, last))
        if self.posts is None:
            raise ModuleError("site unreachable")
        return [{"date": "2026-11-01", **p} for p in self.posts]


@pytest.mark.parametrize("env", [WORDPRESS], indirect=True)
def test_review_warns_about_another_sermon_on_the_date(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts = [{"id": "8216", "title": "Old &lt;sermon&gt;", "link": "https://church.example/old/"}]
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: _FakeSite(posts))
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert "Published outside Georg for this date" in html
    assert '<a href="https://church.example/old/">Old &amp;lt;sermon&amp;gt;</a>' in html


@pytest.mark.parametrize("env", [WORDPRESS], indirect=True)
def test_review_ignores_its_own_publication(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    with session_factory(env.engine).begin() as s:
        pubs = PublicationRepository(s)
        pub = pubs.prepare(env.sermon_id, "website", env.revision_id, "georg:test:sermon-1:website")
        pub.external_id = "8216"
    posts = [{"id": "8216", "title": "Ours", "link": "https://church.example/ours/"}]
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: _FakeSite(posts))
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert "Published outside Georg" not in html


@pytest.mark.parametrize("env", [WORDPRESS], indirect=True)
def test_review_loads_when_the_site_is_unreachable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: _FakeSite(None))
    _login(env)
    response = env.client.get(f"/sermons/{env.sermon_id}")
    assert response.status_code == 200
    assert "Couldn't check the website for another sermon on this date." in response.text


def test_review_without_wordpress_output_does_no_lookup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("no lookup expected")

    monkeypatch.setattr("georg.web.app.build_output", fail)
    _login(env)
    assert "Published outside Georg" not in env.client.get(f"/sermons/{env.sermon_id}").text


def _add_service(env: Env, day: date, ref: str) -> int:
    item = SourceItem(
        source_type="local_folder",
        source_ref=ref,
        parts=(SourcePart(ref=f"{ref}.wav", name=f"{ref}.wav", size=1, checksum=ref),),
        title=ref,
        service_date=day,
    )
    with session_factory(env.engine).begin() as s:
        service, _ = ServiceRepository(s).add_discovered(item)
        return service.id


@pytest.mark.parametrize("env", [WORDPRESS], indirect=True)
def test_services_list_marks_dates_the_website_already_has(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _add_service(env, date(2026, 10, 25), "SPX 10.25.26")
    site = _FakeSite([{"id": "8216", "title": "Old sermon", "link": "https://church.example/old/"}])
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: site)
    _login(env)
    html = env.client.get("/").text
    marker = f'href="https://church.example/old/" data-same-date="{env.service_id}"'
    assert f'<a class="badge warning" {marker}' in html
    assert "Published outside Georg for this date: Old sermon" in html
    assert f'data-same-date="{other}"' not in html
    assert site.calls == [(date(2026, 10, 25), date(2026, 11, 1))]

    env.client.get("/")
    env.client.get(f"/sermons/{env.sermon_id}")
    assert len(site.calls) == 2  # the list reload is cached; the review page asks for its day


@pytest.mark.parametrize("env", [WORDPRESS], indirect=True)
def test_services_list_renders_without_markers_when_the_site_is_unreachable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _FakeSite(None)
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: site)
    _login(env)
    for _ in range(2):
        response = env.client.get("/")
        assert response.status_code == 200
        assert "data-same-date" not in response.text
        assert "SPX 11.01.26" in response.text
    assert len(site.calls) == 1  # the failure is cached too


@pytest.mark.parametrize("env", [WORDPRESS], indirect=True)
def test_service_page_warns_about_another_sermon_on_the_date(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_id = _add_service(env, date(2026, 11, 1), "SPX 11.01.26 second")
    posts = [{"id": "8216", "title": "Old sermon", "link": "https://church.example/old/"}]
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: _FakeSite(posts))
    _login(env)
    html = env.client.get(f"/services/{service_id}").text
    assert "Published outside Georg for this date:" in html
    assert '<a href="https://church.example/old/">Old sermon</a>' in html


def test_review_page_guards_approve_against_unsaved_edits(env: Env) -> None:
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert '<button type="button" id="revert-form" disabled>Revert</button>' in html
    assert 'id="unsaved-note" role="status" hidden' in html
    assert "Save or revert your changes first" in html
    assert '<button type="submit" data-needs-saved>Approve revision 1 and publish</button>' in html
    script = env.client.get("/static/review.js").text
    assert "defaultValue" in script and "data-needs-saved" in script

    _approve(env)
    fragment = env.client.get(f"/sermons/{env.sermon_id}/publish-status").text
    assert '<button type="submit" data-needs-saved >Publish again</button>' in fragment


def test_approve_ignores_unsaved_form_fields(env: Env) -> None:
    _login(env)
    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    env.client.post(
        f"/sermons/{env.sermon_id}/approve",
        data={"csrf_token": token, "revision_id": str(env.revision_id), "preacher": "Unsaved"},
    )
    with session_factory(env.engine).begin() as s:
        sermons = SermonRepository(s)
        revision = sermons.current_revision(env.sermon_id)
        assert revision is not None and revision.id == env.revision_id
        assert sermons.metadata(revision).preacher != "Unsaved"
        approval = sermons.current_approval(env.sermon_id)
        assert approval is not None and approval.revision_id == env.revision_id


def test_unsaved_edits_disable_approve_in_a_browser(env: Env) -> None:
    """Drives review.js in headless Chromium (needs Playwright, e.g.
    `uv run --with playwright pytest -k browser`; GEORG_TEST_CHROME picks the binary)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    _login(env)
    base = "http://testserver"  # the host TestClient renders into static URLs

    def serve(route: Any) -> None:
        url = urlsplit(route.request.url)
        response = env.client.get(url.path + (f"?{url.query}" if url.query else ""))
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=os.environ.get("GEORG_TEST_CHROME"))
        except sync_api.Error as exc:
            pytest.skip(f"no Chromium for Playwright: {exc}")
        page = browser.new_page()
        page.route("**/*", serve)
        page.goto(f"{base}/sermons/{env.sermon_id}")
        approve = page.locator("button[data-needs-saved]")
        revert = page.locator("#revert-form")
        note = page.locator("#unsaved-note")
        preacher = page.locator("input[name=preacher]")
        original = preacher.input_value()
        assert approve.is_enabled() and revert.is_disabled() and note.is_hidden()

        preacher.fill("Someone else")
        assert approve.is_disabled() and revert.is_enabled() and note.is_visible()
        preacher.fill(original)
        assert approve.is_enabled() and revert.is_disabled() and note.is_hidden()

        service_date = page.locator("input[name=service_date]").input_value()
        page.locator("input[name=title]").fill("Edited")
        page.locator("#f-transcript").fill("New words")
        page.locator("input[name=service_date]").fill("2026-11-02")
        assert approve.is_disabled()
        revert.click()
        assert page.locator("input[name=title]").input_value() != "Edited"
        assert page.locator("#f-transcript").input_value() != "New words"
        assert page.locator("input[name=service_date]").input_value() == service_date
        assert approve.is_enabled() and note.is_hidden()
        browser.close()


SITE_AND_PHONE = {
    "outputs": [
        {"name": "website", "type": "wordpress", "options": {}},
        {"name": "phone", "type": "unused"},
    ]
}


class _FakeBridgeSite(_FakeSite):
    def __init__(self, sending: set[str], edited: list[str] | None) -> None:
        super().__init__([])
        self.sending, self.edited = sending, edited
        self.name, self.renditions, self.wants_transcript = "website", frozenset(), True

    def fields_to_send(self, request: Any) -> frozenset[str]:
        assert request.previous is not None
        return frozenset(self.sending)

    def site_status(self, key: str, written_at: datetime | None = None) -> dict[str, Any]:
        if self.edited is None:
            raise ModuleError("site unreachable")
        at = datetime(2026, 11, 13, 16, 55, tzinfo=UTC)
        return {"edited": self.edited, "modified_by": "Owner", "modified_at": at}

    def field_label(self, field: str) -> str:
        return {"terms:ctc_sermon_book": "scripture"}.get(field, field)


def _published_website(env: Env, *, conflict: list[str] | None = None) -> None:
    _publication(env, "website")
    if conflict:
        with session_factory(env.engine).begin() as s:
            pubs = PublicationRepository(s)
            pub = pubs.find(env.sermon_id, "website")
            assert pub is not None
            pubs.mark_conflict(pub.id, "edited", fields=conflict, by="Owner", at=None)


@pytest.mark.parametrize("env", [SITE_AND_PHONE], indirect=True)
def test_published_sermon_approves_without_updating_the_website(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _FakeBridgeSite({"title"}, [])
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: site)
    _published_website(env)
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert ">Approve</button>" in html and "and publish</button>" not in html
    assert "<summary>Update website…</summary>" in html
    assert "Would send: title." in html and "Approve revision 1 first." in html
    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    env.client.post(
        f"/sermons/{env.sermon_id}/approve",
        data={"csrf_token": token, "revision_id": str(env.revision_id)},
    )
    assert _jobs(env, "publish") == [{"sermon_id": env.sermon_id, "targets": ["phone"]}]

    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert ">Update website</button>" in html and "Skip these fields" not in html
    env.client.post(
        f"/sermons/{env.sermon_id}/publish", data={"csrf_token": token, "website": "website"}
    )
    assert _jobs(env, "publish")[-1] == {"sermon_id": env.sermon_id, "targets": ["website"]}


@pytest.mark.parametrize("env", [SITE_AND_PHONE], indirect=True)
def test_conflict_shows_who_edited_and_offers_skip_or_overwrite(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _FakeBridgeSite({"title", "terms:ctc_sermon_book"}, ["terms:ctc_sermon_book"])
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: site)
    _approve(env)
    _published_website(env, conflict=["terms:ctc_sermon_book"])
    _login(env)
    html = env.client.get(f"/sermons/{env.sermon_id}").text
    assert 'data-output="website">Website edited: needs decision<' in html
    assert re.search(r"Content was edited on the website \(Nov 13, 2026 [^,]+, by Owner\)", html)
    assert "by Owner): scripture." in html
    assert "Would send: scripture, title." in html
    assert ">Skip these fields</button>" in html and ">Overwrite on website</button>" in html
    assert 'name="confirm" value="yes" required' in html

    token = _csrf(env.client, f"/sermons/{env.sermon_id}")
    form = {"csrf_token": token, "website": "website", "fields": "terms:ctc_sermon_book"}
    env.client.post(f"/sermons/{env.sermon_id}/publish", data={**form, "decision": "skip"})
    assert _jobs(env, "publish")[-1] == {
        "sermon_id": env.sermon_id,
        "targets": ["website"],
        "skip": ["terms:ctc_sermon_book"],
    }
    response = env.client.post(
        f"/sermons/{env.sermon_id}/publish", data={**form, "decision": "overwrite"}
    )
    assert "Confirm that the website&#39;s edits may be overwritten." in response.text
    assert len(_jobs(env, "publish")) == 1
    env.client.post(
        f"/sermons/{env.sermon_id}/publish",
        data={**form, "decision": "overwrite", "confirm": "yes"},
    )
    assert _jobs(env, "publish")[-1] == {
        "sermon_id": env.sermon_id,
        "targets": ["website"],
        "overwrite": ["terms:ctc_sermon_book"],
    }


@pytest.mark.parametrize("env", [SITE_AND_PHONE], indirect=True)
def test_recorded_conflict_shows_when_the_website_is_unreachable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _FakeBridgeSite(set(), None)
    monkeypatch.setattr("georg.web.app.build_output", lambda *a, **k: site)
    _approve(env)
    _published_website(env, conflict=["title"])
    _login(env)
    response = env.client.get(f"/sermons/{env.sermon_id}")
    assert response.status_code == 200
    assert "Couldn't check the website for edits." in response.text
    assert "Content was edited on the website (by Owner): title." in response.text
    assert ">Skip these fields</button>" in response.text
