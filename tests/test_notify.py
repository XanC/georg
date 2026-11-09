# SPDX-License-Identifier: AGPL-3.0-only
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy import Engine

from georg.core.config import GeorgConfig, ReviewConfig, SmtpConfig
from georg.core.errors import ModuleError
from georg.core.types import SermonMetadata, ServiceBoundaries, SourceItem, SourcePart
from georg.db.engine import session_factory
from georg.db.repositories import SermonRepository, ServiceRepository
from georg.notify import notify_review, redact


class FakeSmtp:
    sent: ClassVar[list[EmailMessage]] = []
    fail: ClassVar[Exception | None] = None

    def __init__(self, host: str, port: int) -> None:
        self.calls: list[Any] = [(host, port)]

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def starttls(self) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        if FakeSmtp.fail:
            raise FakeSmtp.fail

    def send_message(self, message: EmailMessage) -> None:
        FakeSmtp.sent.append(message)


@pytest.fixture
def config(tmp_path: Path) -> GeorgConfig:
    return GeorgConfig.model_validate({
        "instance": "test",
        "data_dir": tmp_path / "data",
        "inputs": [{"name": "drive", "type": "local_folder", "options": {"root": tmp_path}}],
        "processors": {k: {"type": "unused"} for k in
                       ("classifier", "detector", "transcriber", "formatter", "metadata")},
    })  # fmt: skip


def _config(config: GeorgConfig, notify: tuple[str, ...]) -> GeorgConfig:
    review = ReviewConfig(
        notify=notify,
        base_url="https://georg.example.org/",
        smtp=SmtpConfig(host="smtp.example.org", username="u", password="p", sender="g@x.org"),
    )
    return config.model_copy(update={"review": review})


def _service(engine: Engine) -> int:
    sessions = session_factory(engine)
    with sessions.begin() as s:
        part = SourcePart(ref="1.mp4", name="1.mp4")
        item = SourceItem(source_type="local_folder", source_ref="a", parts=(part,))
        service, _ = ServiceRepository(s).add_discovered(item)
        sermons = SermonRepository(s)
        sermon = sermons.create(service.id)
        sermons.add_revision(
            sermon.id,
            author="system",
            boundaries=ServiceBoundaries(
                service_start_ms=60_000,
                sermon_start_ms=1_500_000,
                sermon_end_ms=2_700_000,
                service_end_ms=4_000_000,
            ),
            metadata=SermonMetadata(occasion="All Saints Day", preacher="Eric"),
            transcript_markdown="Hello.",
        )
        return service.id


def test_sends_notice_to_every_reviewer(config: GeorgConfig, engine: Engine) -> None:
    FakeSmtp.sent, FakeSmtp.fail = [], None
    config = _config(config, ("a@x.org", "b@x.org"))
    notify_review(config, session_factory(engine), _service(engine), factory=FakeSmtp)
    [message] = FakeSmtp.sent
    assert message["To"] == "a@x.org, b@x.org"
    assert message["Subject"].startswith("Ready for review: All Saints Day")
    body = message.get_content()
    assert "Preacher: Eric" in body
    assert "Sermon: 0:25:00 to 0:45:00 (20 min)" in body
    assert "https://georg.example.org/sermons/1" in body


def test_no_recipients_sends_nothing(config: GeorgConfig, engine: Engine) -> None:
    FakeSmtp.sent = []
    notify_review(_config(config, ()), session_factory(engine), _service(engine), factory=FakeSmtp)
    assert FakeSmtp.sent == []


def test_smtp_errors(config: GeorgConfig, engine: Engine) -> None:
    config, sessions = _config(config, ("a@x.org",)), session_factory(engine)
    service_id = _service(engine)
    FakeSmtp.fail = smtplib.SMTPAuthenticationError(535, b"no")
    with pytest.raises(ModuleError) as err:
        notify_review(config, sessions, service_id, factory=FakeSmtp)
    assert not err.value.retryable
    FakeSmtp.fail = ConnectionRefusedError()
    with pytest.raises(ModuleError) as err:
        notify_review(config, sessions, service_id, factory=FakeSmtp)
    assert err.value.retryable
    FakeSmtp.fail = None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("GET https://u:pw@h.example.org:8443/a/b?access_token=t0k#frag failed.",
         "GET https://h.example.org:8443/a/b?[redacted] failed."),
        ("(see https://h.example.org/x)", "(see https://h.example.org/x)"),
        ("Bearer ya29.abc-def", "Bearer [redacted]"),
        ("Authorization: Bearer ya29.abc", "Authorization: [redacted] [redacted]"),
        ("refresh_token=rtok&client_secret=cs",
         "refresh_token=[redacted]&client_secret=[redacted]"),
        ('{"api_key": "sk-123", "x": 1}', '{"api_key": "[redacted]", "x": 1}'),
        ("used hunter22 to log in", "used [redacted] to log in"),
        ("run `georg drive-login`: revoked", "run `georg drive-login`: revoked"),
    ],
)  # fmt: skip
def test_redact(text: str, expected: str) -> None:
    assert redact(text, ["hunter22", "ab"]) == expected
