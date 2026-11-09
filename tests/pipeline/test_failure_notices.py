# SPDX-License-Identifier: AGPL-3.0-only
import json
import smtplib
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine
from test_notify import FakeSmtp

from georg.core.config import GeorgConfig, ReviewConfig, SmtpConfig
from georg.core.errors import ModuleError
from georg.core.types import SermonMetadata, ServiceBoundaries, SourceItem, SourcePart
from georg.db.engine import session_factory
from georg.db.repositories import PublicationRepository, SermonRepository, ServiceRepository
from georg.jobs.queue import JobQueue
from georg.pipeline import Worker


@pytest.fixture(autouse=True)
def smtp() -> Iterator[type[FakeSmtp]]:
    FakeSmtp.sent, FakeSmtp.fail = [], None
    yield FakeSmtp
    FakeSmtp.sent, FakeSmtp.fail = [], None


def _config(tmp_path: Path, *, notify: tuple[str, ...] = ("a@x.org", "b@x.org")) -> GeorgConfig:
    return GeorgConfig.model_validate({
        "instance": "test",
        "data_dir": tmp_path / "data",
        "inputs": [{"name": "drive", "type": "local_folder", "options": {"root": tmp_path}}],
        "processors": {k: {"type": "unused"} for k in
                       ("classifier", "detector", "transcriber", "formatter", "metadata")},
        "outputs": [
            {"name": "website", "type": "wordpress", "renditions": ["sermon"],
             "options": {"base_url": "https://www.example.org"}},
            {"name": "phone", "type": "sftp", "renditions": ["service_phone"],
             "options": {"host": "ssh.example.org", "remote_path": "/x/{date}.mp3"}},
        ],
        "review": ReviewConfig(
            notify=notify,
            base_url="https://georg.example.org/",
            smtp=SmtpConfig(host="smtp.example.org", username="u", password="p",
                            sender="Georg <g@x.org>"),
        ),
    })  # fmt: skip


@pytest.fixture
def config(tmp_path: Path) -> GeorgConfig:
    return _config(tmp_path)


def _service(engine: Engine) -> tuple[int, int, int]:
    """A service with a dated sermon revision: (service id, sermon id, revision id)."""
    with session_factory(engine).begin() as s:
        part = SourcePart(ref="1.mp4", name="1.mp4")
        item = SourceItem(source_type="local_folder", source_ref="SPX 11.01.26", parts=(part,))
        service, _ = ServiceRepository(s).add_discovered(item)
        sermons = SermonRepository(s)
        sermon = sermons.create(service.id)
        revision = sermons.add_revision(
            sermon.id,
            author="system",
            boundaries=ServiceBoundaries(
                service_start_ms=0, sermon_start_ms=1, sermon_end_ms=2, service_end_ms=3
            ),
            metadata=SermonMetadata(occasion="All Saints Day", service_date=date(2026, 11, 1)),
            transcript_markdown="Hello.",
        )
        return service.id, sermon.id, revision.id


def _worker(
    config: GeorgConfig, engine: Engine, queue: JobQueue, kind: str, error: Exception
) -> Worker:
    worker = Worker(config, engine, queue=queue, worker_id="w", smtp_factory=FakeSmtp)

    def fail(_: dict[str, Any]) -> None:
        raise error

    worker.handlers[kind] = fail
    return worker


def test_permanent_failure_sends_one_email(config: GeorgConfig, engine: Engine) -> None:
    service_id, _, _ = _service(engine)
    queue = JobQueue(engine)
    job_id = queue.enqueue("process", {"service_id": service_id})
    worker = _worker(config, engine, queue, "process", ModuleError("ffmpeg exited with 1"))

    assert worker.run_once()
    [message] = FakeSmtp.sent
    assert message["Subject"] == "Georg: process failed for 2026-11-01"
    assert message["To"] == "a@x.org, b@x.org"
    body = message.get_content()
    assert "Error: ModuleError: ffmpeg exited with 1" in body
    assert f"Service: All Saints Day, 2026-11-01 (service {service_id})" in body
    assert "Attempts: 1 of 5" in body
    assert f"Review: https://georg.example.org/services/{service_id}" in body
    assert "Outputs:" not in body
    job = queue.get(job_id)
    assert job.status == "failed" and job.failure_notified_at is not None


def test_retries_exhausted_sends_one_email(config: GeorgConfig, engine: Engine) -> None:
    service_id, _, _ = _service(engine)
    now = [datetime(2026, 11, 1, 18, tzinfo=UTC)]
    queue = JobQueue(engine, max_attempts=2, clock=lambda: now[0])
    job_id = queue.enqueue("process", {"service_id": service_id})
    worker = _worker(config, engine, queue, "process", ModuleError("timeout", retryable=True))

    assert worker.run_once()
    assert queue.get(job_id).status == "queued"
    assert FakeSmtp.sent == []

    now[0] += timedelta(hours=1)
    assert worker.run_once()
    assert queue.get(job_id).status == "failed"
    [message] = FakeSmtp.sent
    assert "Attempts: 2 of 2" in message.get_content()


def test_retryable_failure_sends_nothing(config: GeorgConfig, engine: Engine) -> None:
    queue = JobQueue(engine)
    queue.enqueue("discover")
    worker = _worker(config, engine, queue, "discover", ModuleError("503", retryable=True))
    assert worker.run_once()
    worker.notify_failures()
    assert queue.list()[0].status == "queued"
    assert FakeSmtp.sent == []


def test_no_duplicate_after_reprocessing_or_restart(config: GeorgConfig, engine: Engine) -> None:
    service_id, _, _ = _service(engine)
    queue = JobQueue(engine)
    queue.enqueue("process", {"service_id": service_id})
    worker = _worker(config, engine, queue, "process", ModuleError("broken"))
    assert worker.run_once()
    assert not worker.run_once()
    worker.notify_failures()

    restarted = _worker(config, engine, JobQueue(engine), "process", ModuleError("broken"))
    stop = threading.Event()

    def reclaim_then_stop() -> int:
        stop.set()
        return 0

    restarted.queue.reclaim_stale = reclaim_then_stop  # type: ignore[method-assign]
    restarted.run(poll_seconds=0, stop=stop)
    restarted.notify_failures()
    assert len(FakeSmtp.sent) == 1


def test_expired_lease_without_attempts_left_is_reported(
    config: GeorgConfig, engine: Engine
) -> None:
    now = [datetime(2026, 11, 1, 18, tzinfo=UTC)]
    queue = JobQueue(engine, max_attempts=1, clock=lambda: now[0])
    job_id = queue.enqueue("discover")
    assert queue.lease("crashed") is not None
    now[0] += timedelta(hours=1)
    assert queue.reclaim_stale() == 1
    worker = Worker(config, engine, queue=queue, worker_id="w", smtp_factory=FakeSmtp)
    worker.notify_failures()
    worker.notify_failures()
    [message] = FakeSmtp.sent
    assert message["Subject"] == "Georg: discover failed"
    body = message.get_content()
    assert "lease expired for worker crashed" in body
    assert "Review: https://georg.example.org/jobs" in body
    assert queue.get(job_id).status == "failed"


def test_smtp_failure_is_logged_and_swallowed(
    config: GeorgConfig, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    FakeSmtp.fail = smtplib.SMTPServerDisconnected("gone")
    queue = JobQueue(engine)
    job_id = queue.enqueue("discover")
    worker = _worker(config, engine, queue, "discover", ModuleError("broken"))
    assert worker.run_once()
    assert FakeSmtp.sent == []
    assert "could not email reviewers" in caplog.text
    job = queue.get(job_id)
    assert job.status == "failed" and job.last_error == "ModuleError: broken"
    FakeSmtp.fail = None
    worker.notify_failures()
    assert FakeSmtp.sent == []


@pytest.mark.parametrize("notify, smtp", [((), True), (("a@x.org",), False)])
def test_without_email_settings_failures_are_marked_reported(
    tmp_path: Path, engine: Engine, notify: tuple[str, ...], smtp: bool
) -> None:
    config = _config(tmp_path, notify=notify)
    if not smtp:
        review = config.review.model_copy(update={"smtp": None})
        config = config.model_copy(update={"review": review})
    queue = JobQueue(engine)
    job_id = queue.enqueue("discover")
    worker = _worker(config, engine, queue, "discover", ModuleError("broken"))
    assert worker.run_once()
    assert FakeSmtp.sent == []
    job = queue.get(job_id)
    assert job.status == "failed" and job.failure_notified_at is not None

    enabled = Worker(_config(tmp_path), engine, queue=queue, smtp_factory=FakeSmtp)
    enabled.notify_failures()
    assert FakeSmtp.sent == []


def test_publish_failure_names_outputs_without_secrets(
    config: GeorgConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEORG_TEST_WP_KEY", "s3cr3t-hmac-key")
    outputs = list(config.outputs)
    outputs[0] = outputs[0].model_copy(
        update={"options": {**outputs[0].options, "key": "env:GEORG_TEST_WP_KEY"}}
    )
    config = config.model_copy(update={"outputs": tuple(outputs)})
    service_id, sermon_id, revision_id = _service(engine)
    sessions = session_factory(engine)
    error = (
        "HTTP 500 from https://user:pw@www.example.org/wp-json/georg/v1/sermons"
        "?signature=abc123&key_id=2026-11 (key s3cr3t-hmac-key)"
    )
    with sessions.begin() as s:
        pubs = PublicationRepository(s)
        pub = pubs.prepare(sermon_id, "website", revision_id, "georg:test:sermon-1:website")
        pubs.mark_failed(pub.id, error)
    queue = JobQueue(engine)
    queue.enqueue("publish", {"sermon_id": sermon_id})
    worker = _worker(config, engine, queue, "publish", ModuleError(f"website: {error}"))

    assert worker.run_once()
    [message] = FakeSmtp.sent
    assert message["Subject"] == "Georg: publish failed for 2026-11-01"
    body = message.get_content()
    assert "  website: failed: HTTP 500 from https://www.example.org/wp-json/" in body
    assert "  phone: not published" in body
    assert f"Review: https://georg.example.org/services/{service_id}" in body
    for secret in ("s3cr3t-hmac-key", "abc123", "pw@", "user:"):
        assert secret not in body


def test_revoked_drive_sign_in_hints_drive_login(
    config: GeorgConfig, engine: Engine, tmp_path: Path
) -> None:
    pytest.importorskip("google.oauth2.credentials")
    from georg.gdrive import SCOPE_READONLY, GoogleTokens

    user = tmp_path / "user.json"
    user.write_text(
        json.dumps({
            "type": "authorized_user",
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "csecret",
            "refresh_token": "rtok",
            "token_uri": "https://oauth2.example.test/token",
        })
    )  # fmt: skip

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad"})

    tokens = GoogleTokens(
        str(user), [SCOPE_READONLY], http_client=httpx.Client(transport=httpx.MockTransport(refuse))
    )
    queue = JobQueue(engine)
    queue.enqueue("discover")
    worker = Worker(config, engine, queue=queue, worker_id="w", smtp_factory=FakeSmtp)
    worker.handlers["discover"] = lambda _: tokens.token()

    assert worker.run_once()
    assert queue.list()[0].status == "failed"
    [message] = FakeSmtp.sent
    body = message.get_content()
    assert "run `georg drive-login`" in body
    assert "csecret" not in body and "rtok" not in body


def test_drive_login_hint_survives_redaction(config: GeorgConfig, engine: Engine) -> None:
    error = ModuleError(
        "Google token refresh failed: ('invalid_grant: Token has been expired or revoked.', "
        "{'error': 'invalid_grant'}); the sign-in may have been revoked: run `georg drive-login`"
    )
    queue = JobQueue(engine)
    queue.enqueue("discover")
    assert _worker(config, engine, queue, "discover", error).run_once()
    [message] = FakeSmtp.sent
    body = message.get_content()
    assert "Token has been expired or revoked." in body
    assert "the sign-in may have been revoked: run `georg drive-login`" in body
