# SPDX-License-Identifier: AGPL-3.0-only
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from georg.core.checks import CheckResult
from georg.core.config import GeorgConfig
from georg.core.types import SermonMetadata, ServiceBoundaries, SourceItem, SourcePart
from georg.db.engine import session_factory
from georg.db.repositories import (
    AuditRepository,
    SermonRepository,
    ServiceRepository,
    UserRepository,
)
from georg.jobs.queue import JobQueue
from georg.storage.local import LocalStore
from georg.web import admin, create_app
from georg.web.admin import FormProblem
from georg.web.auth import CurrentUser, hash_password

PASSWORD = "correct horse battery"
ENV_SECRET = "env-secret-value-1234"
LITERAL_SECRET = "literal-password-5678"
BOUNDS = ServiceBoundaries(
    service_start_ms=60_000, sermon_start_ms=1_200_000, sermon_end_ms=2_400_000,
    service_end_ms=3_600_000,
)  # fmt: skip


@dataclass
class Env:
    client: TestClient
    queue: JobQueue
    engine: Engine
    sermon_id: int
    revision_id: int
    ids: dict[str, int]


def _config(tmp_path: Path) -> GeorgConfig:
    return GeorgConfig.model_validate({
        "instance": "test",
        "data_dir": tmp_path / "data",
        "inputs": [{"name": "drive", "type": "google_drive",
                    "options": {"folder_id": "env:GEORG_TEST_FOLDER",
                                "credentials_file": "env:GEORG_TEST_MISSING"}}],
        "processors": {k: {"type": "unused", "options": {"api_key": "env:GEORG_TEST_KEY"}}
                       for k in ("classifier", "detector", "transcriber", "formatter", "metadata")},
        "outputs": [
            {"name": "website", "type": "wordpress", "renditions": ["sermon"],
             "options": {"base_url": "https://church.example", "key_id": "2026-11",
                         "key": "env:GEORG_TEST_KEY"}},
            {"name": "phone", "type": "sftp", "renditions": ["service_phone"],
             "options": {"host": "web.example", "username": "georg",
                         "password": LITERAL_SECRET, "remote_path": "/srv/{date}.mp3"}},
            {"name": "other", "type": "unused"},
        ],
        "review": {"smtp": {"host": "smtp.example", "username": "georg",
                            "password": LITERAL_SECRET, "sender": "georg@example.org"}},
    })  # fmt: skip


@pytest.fixture
def env(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("GEORG_TEST_KEY", ENV_SECRET)
    monkeypatch.setenv("GEORG_TEST_FOLDER", "private-folder-id-42")
    monkeypatch.delenv("GEORG_TEST_MISSING", raising=False)
    config = _config(tmp_path)
    item = SourceItem(
        source_type="local_folder",
        source_ref="SPX 11.01.26",
        parts=(SourcePart(ref="1.wav", name="1.wav", size=1, checksum="x"),),
        title="SPX 11.01.26",
        service_date=date(2026, 11, 1),
    )
    with session_factory(engine).begin() as s:
        users = UserRepository(s)
        ids = {
            name: users.add(name, hash_password(PASSWORD), role).id
            for name, role in (("root", "admin"), ("rev", "reviewer"), ("view", "viewer"))
        }
        service, _ = ServiceRepository(s).add_discovered(item)
        sermons = SermonRepository(s)
        sermon = sermons.create(service.id)
        first = sermons.add_revision(
            sermon.id, author="system", boundaries=BOUNDS,
            metadata=SermonMetadata(title="First"), transcript_markdown="x",
        )  # fmt: skip
        sermons.add_revision(
            sermon.id, author="system", boundaries=BOUNDS,
            metadata=SermonMetadata(title="Second"), transcript_markdown="y",
        )  # fmt: skip
        sermon_id, first_id = sermon.id, first.id
    queue = JobQueue(engine)
    store = LocalStore.for_data_dir(config.data_dir)
    app = create_app(config, engine, secret_key="test-secret", store=store, queue=queue)
    with TestClient(app) as client:
        yield Env(client, queue, engine, sermon_id, first_id, ids)


def _csrf(client: TestClient, path: str = "/account") -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match is not None
    return match.group(1)


def _login(client: TestClient, username: str, password: str = PASSWORD) -> Any:
    token = _csrf(client, "/login")
    return client.post(
        "/login", data={"username": username, "password": password, "csrf_token": token}
    )


def _post(env: Env, path: str, **data: str) -> Any:
    return env.client.post(path, data={"csrf_token": _csrf(env.client), **data})


def _audit(env: Env) -> list[tuple[str, str | None]]:
    with session_factory(env.engine).begin() as s:
        return [(e.action, e.target) for e in AuditRepository(s).recent()]


def _editor_posts(env: Env) -> list[tuple[str, dict[str, str]]]:
    sid = env.sermon_id
    return [
        (f"/sermons/{sid}/revisions", {"base_revision_id": "0"}),
        (f"/sermons/{sid}/revisions/{env.revision_id}/restore", {}),
        (f"/sermons/{sid}/approve", {"revision_id": str(env.revision_id)}),
        (f"/sermons/{sid}/reprocess", {}),
        (f"/sermons/{sid}/publish", {}),
        ("/jobs/discover", {}),
    ]


def _admin_posts(env: Env) -> list[tuple[str, dict[str, str]]]:
    rev = env.ids["rev"]
    return [
        ("/admin/users", {"username": "eve", "role": "admin"}),
        (f"/admin/users/{rev}/role", {"role": "admin"}),
        (f"/admin/users/{rev}/disable", {}),
        (f"/admin/users/{rev}/enable", {}),
        (f"/admin/users/{rev}/email", {"email": "x@example.org"}),
        (f"/admin/users/{rev}/password", {}),
        ("/admin/checks/output/website", {}),
        ("/admin/checks/email/smtp", {}),
        ("/admin/checks/email/me", {}),
    ]


def test_viewer_gets_403_on_every_changing_route(env: Env) -> None:
    _login(env.client, "view")
    for path, data in _editor_posts(env) + _admin_posts(env):
        response = _post(env, path, **data)
        assert response.status_code == 403, path
    assert env.queue.list() == []
    with session_factory(env.engine).begin() as s:
        assert len(SermonRepository(s).revisions(env.sermon_id)) == 2
        assert UserRepository(s).by_username("eve") is None
    assert _audit(env) == []


def test_reviewer_can_edit_but_not_administer(env: Env) -> None:
    _login(env.client, "rev")
    assert env.client.get("/admin").status_code == 403
    assert env.client.get("/admin/settings").status_code == 403
    for path, data in _admin_posts(env):
        assert _post(env, path, **data).status_code == 403, path
    assert _post(env, "/jobs/discover").status_code == 200
    assert 'href="/admin"' not in env.client.get("/").text


def test_viewer_sees_pages_without_controls(env: Env) -> None:
    _login(env.client, "view")
    review = env.client.get(f"/sermons/{env.sermon_id}").text
    assert "Save as new revision" not in review
    assert "and publish</button>" not in review
    assert "/restore" not in review
    assert 'id="read-only"' in review
    assert "Check for new recordings" not in env.client.get("/jobs").text
    assert env.client.get(f"/sermons/{env.sermon_id}/publish-status").status_code == 200


def test_editor_controls_survive_publish_status_polling(env: Env) -> None:
    _login(env.client, "rev")
    fragment = env.client.get(f"/sermons/{env.sermon_id}/publish-status").text
    assert "Approve revision 2 and publish" in fragment


def test_admin_adds_a_user_with_a_generated_password(env: Env) -> None:
    _login(env.client, "root")
    page = env.client.get("/")
    assert 'href="/admin"' in page.text
    response = _post(env, "/admin/users", username="newbie", role="viewer", email="n@example.org")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    match = re.search(
        r"Temporary password for <strong>newbie</strong>: <code>([^<]+)</code>", response.text
    )
    assert match is not None
    temporary = match.group(1)
    assert temporary not in env.client.get("/admin/users").text
    assert ("user.add", "newbie") in _audit(env)

    other = TestClient(env.client.app)
    assert _login(other, "newbie", temporary).status_code == 200
    with session_factory(env.engine).begin() as s:
        user = UserRepository(s).by_username("newbie")
        assert user is not None and user.role == "viewer" and user.last_login_at is not None
        assert user.email == "n@example.org"
    assert "newbie" in env.client.get("/admin").text


def test_admin_add_user_validation(env: Env) -> None:
    _login(env.client, "root")
    typed = _post(env, "/admin/users", username="pat", role="reviewer", password="long enough")
    assert typed.status_code == 200 and "Temporary password" not in typed.text
    assert "already exists" in _post(env, "/admin/users", username="pat", role="viewer").text
    assert (
        "at least 8"
        in _post(env, "/admin/users", username="kim", role="viewer", password="short").text
    )
    assert _post(env, "/admin/users", username="bad name", role="viewer").status_code == 400
    assert _post(env, "/admin/users", username="kim", role="owner").status_code == 400
    assert (
        "look like an email address"
        in _post(env, "/admin/users", username="kim", role="viewer", email="nope").text
    )


def test_admin_changes_role_email_and_password(env: Env) -> None:
    _login(env.client, "root")
    rev = env.ids["rev"]
    _post(env, f"/admin/users/{rev}/role", role="viewer")
    _post(env, f"/admin/users/{rev}/email", email="rev@example.org")
    _post(env, f"/admin/users/{rev}/password", password="brand new password")
    reset = _post(env, f"/admin/users/{rev}/password")
    assert "Temporary password for <strong>rev</strong>" in reset.text
    with session_factory(env.engine).begin() as s:
        user = UserRepository(s).get(rev)
        assert user.role == "viewer" and user.email == "rev@example.org"
    actions = [a for a, _ in _audit(env)]
    assert actions[:4] == ["user.password_reset", "user.password_reset", "user.email", "user.role"]
    assert "brand new password" not in env.client.get("/admin").text


def test_admin_cannot_disable_or_demote_themselves(env: Env) -> None:
    _login(env.client, "root")
    root = env.ids["root"]
    assert "disable or demote yourself" in _post(env, f"/admin/users/{root}/disable").text
    assert (
        "disable or demote yourself"
        in _post(env, f"/admin/users/{root}/role", role="reviewer").text
    )
    with session_factory(env.engine).begin() as s:
        user = UserRepository(s).get(root)
        assert user.role == "admin" and user.disabled_at is None


def test_last_active_admin_is_kept(env: Env) -> None:
    other = CurrentUser(999, "someone", "admin")
    with session_factory(env.engine).begin() as s:
        users = UserRepository(s)
        with pytest.raises(FormProblem, match="last active admin"):
            admin._guard(users, other, env.ids["root"], removes_admin=True)
        second = users.add("root2", hash_password(PASSWORD), "admin")
        admin._guard(users, other, env.ids["root"], removes_admin=True)
        users.set_disabled(second.id, users.get(env.ids["root"]).created_at)
        with pytest.raises(FormProblem, match="last active admin"):
            admin._guard(users, other, env.ids["root"], removes_admin=True)


def test_disabled_user_is_logged_out_and_cannot_log_in(env: Env) -> None:
    reviewer = TestClient(env.client.app)
    _login(reviewer, "rev")
    assert reviewer.get("/").status_code == 200

    _login(env.client, "root")
    _post(env, f"/admin/users/{env.ids['rev']}/disable")
    response = reviewer.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    refused = _login(reviewer, "rev")
    assert refused.status_code == 403 and "This account is disabled." in refused.text

    _post(env, f"/admin/users/{env.ids['rev']}/enable")
    assert _login(reviewer, "rev").status_code == 200
    assert ("user.disable", "rev") in _audit(env)


def test_account_password_change(env: Env) -> None:
    _login(env.client, "view")
    wrong = _post(
        env, "/account/password", current="nope", new="new password!", confirm="new password!"
    )
    assert wrong.status_code == 400 and "current password is wrong" in wrong.text
    mismatch = _post(
        env, "/account/password", current=PASSWORD, new="new password!", confirm="other"
    )
    assert "The new passwords don" in mismatch.text
    ok = _post(
        env, "/account/password", current=PASSWORD, new="new password!", confirm="new password!"
    )
    assert ok.status_code == 200 and "Your password was changed." in ok.text
    assert _post(env, "/account/email", email="v@example.org").status_code == 200
    assert _audit(env)[:2] == [("account.email", "view"), ("account.password", "view")]
    fresh = TestClient(env.client.app)
    assert _login(fresh, "view").status_code == 401
    assert _login(fresh, "view", "new password!").status_code == 200


def test_settings_page_never_shows_secrets(env: Env) -> None:
    _login(env.client, "root")
    html = env.client.get("/admin/settings").text
    assert ENV_SECRET not in html
    assert LITERAL_SECRET not in html
    assert "private-folder-id-42" not in html
    assert "env:GEORG_TEST_KEY (set)" in html
    assert "env:GEORG_TEST_FOLDER (set)" in html
    assert "env:GEORG_TEST_MISSING (missing)" in html
    assert "(hidden)" in html
    for section in ("Inputs", "Processors", "Renditions", "Outputs", "Review", "Email (SMTP)",
                    "Discovery", "Calendar", "Idle shutdown"):  # fmt: skip
        assert f">{section}</h2>" in html
    assert 'hx-post="/admin/checks/output/website"' in html
    assert 'hx-post="/admin/checks/input/drive"' in html
    assert 'hx-post="/admin/checks/output/other"' not in html
    assert "No test available." in html


class _Fake:
    def __init__(self, result: CheckResult | None = None, delay: float = 0) -> None:
        self.result = result
        self.delay = delay

    def check(self) -> CheckResult:
        time.sleep(self.delay)
        assert self.result is not None
        return self.result


def test_connection_check_results_are_redacted(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    leaky = CheckResult(False, f"denied for {ENV_SECRET} at https://x.example/a?token=abc")
    monkeypatch.setattr(admin, "build_output", lambda *a, **k: _Fake(leaky))
    _login(env.client, "root")
    html = _post(env, "/admin/checks/output/website").text
    assert "Failed" in html and ENV_SECRET not in html and "token=abc" not in html
    assert ("check.run", "output:website") in _audit(env)

    monkeypatch.setattr(admin, "build_output", lambda *a, **k: object())
    assert "No test available." in _post(env, "/admin/checks/output/other").text

    def broken(*a: object, **k: object) -> object:
        raise RuntimeError(f"cannot start with {LITERAL_SECRET}")

    monkeypatch.setattr(admin, "build_input", broken)
    html = _post(env, "/admin/checks/input/drive").text
    assert "set up the module" in html and LITERAL_SECRET not in html
    assert _post(env, "/admin/checks/output/nope").status_code == 404


def test_connection_check_times_out(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "CHECK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        admin, "build_output", lambda *a, **k: _Fake(CheckResult(True, "late"), 0.5)
    )
    _login(env.client, "root")
    assert "No answer within" in _post(env, "/admin/checks/output/website").text


class FakeSmtp:
    sent: ClassVar[list[EmailMessage]] = []
    logins: ClassVar[list[str]] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def starttls(self) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        FakeSmtp.logins.append(user)

    def send_message(self, message: EmailMessage) -> None:
        FakeSmtp.sent.append(message)


def test_smtp_check_and_test_email(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSmtp.sent.clear()
    FakeSmtp.logins.clear()
    monkeypatch.setattr("smtplib.SMTP", FakeSmtp)
    _login(env.client, "root")
    html = _post(env, "/admin/checks/email/smtp").text
    assert "OK" in html and "STARTTLS, logged in; nothing was sent." in html
    assert FakeSmtp.sent == [] and FakeSmtp.logins == ["georg"]

    assert "Account page first" in _post(env, "/admin/checks/email/me").text
    _post(env, "/account/email", email="root@example.org")
    assert "Send test email to me" in env.client.get("/admin/settings").text
    assert "Test email sent to root@example.org." in _post(env, "/admin/checks/email/me").text
    (message,) = FakeSmtp.sent
    assert message["To"] == "root@example.org"


def test_admin_home_shows_health_and_audit(env: Env) -> None:
    job_id = env.queue.enqueue("discover", {})
    env.queue.lease("w")
    env.queue.fail(job_id, "w", "boom", retryable=False)
    env.queue.enqueue("discover", {})
    _login(env.client, "root")
    _post(env, f"/admin/users/{env.ids['rev']}/email", email="rev@example.org")
    html = env.client.get("/admin").text
    assert 'data-count="failed">failed 1<' in html
    assert 'data-count="queued">queued 1<' in html
    assert f'<a href="/jobs">#{job_id} discover</a>' in html
    assert "Data directory" in html and "Root filesystem" in html
    assert "Last job finished: 20" in html
    assert "user.email" in html and ">root<" in html
