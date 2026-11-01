# SPDX-License-Identifier: AGPL-3.0-only
"""Email notices to reviewers."""

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from georg.core.config import GeorgConfig, SmtpConfig, resolve_secret
from georg.core.errors import ConfigError, ModuleError
from georg.db.repositories import SermonRepository, ServiceRepository

SmtpFactory = Callable[[str, int], Any]
"""Returns a context manager behaving like `smtplib.SMTP`."""


def _hms(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def review_notice(
    config: GeorgConfig, sessions: sessionmaker[Session], service_id: int
) -> EmailMessage:
    """The "ready for review" email for the service's current sermon revision."""
    review = config.review
    if review.smtp is None:
        raise ConfigError("review.notify is set but review.smtp is not configured")
    with sessions() as session:
        service = ServiceRepository(session).get(service_id)
        sermons = SermonRepository(session)
        found = sermons.for_service(service_id)
        revision = sermons.current_revision(found[0].id) if found else None
        if revision is None:
            raise ModuleError(f"service {service_id} has no sermon to review")
        meta = sermons.metadata(revision)
        b = sermons.boundaries(revision)
        sermon_id = found[0].id
        day = meta.service_date or service.service_date
    label = meta.title or meta.occasion or "Service"
    when = day.isoformat() if day else "undated"
    lines = [
        f"{label} ({when}) is ready for review.",
        "",
        *(
            f"{k}: {v}"
            for k, v in (("Preacher", meta.preacher), ("Scripture", meta.scripture))
            if v
        ),
        f"Sermon: {_hms(b.sermon_start_ms)} to {_hms(b.sermon_end_ms)}"
        f" ({(b.sermon_end_ms - b.sermon_start_ms) // 60000} min)",
        f"Service: {_hms(b.service_start_ms)} to {_hms(b.service_end_ms)}",
    ]
    if review.base_url:
        lines += ["", f"Review: {review.base_url.rstrip('/')}/sermons/{sermon_id}"]
    message = EmailMessage()
    message["Subject"] = f"Ready for review: {label}, {when}"
    message["From"] = review.smtp.sender
    message["To"] = ", ".join(review.notify)
    message.set_content("\n".join(lines) + "\n")
    return message


def send(message: EmailMessage, smtp: SmtpConfig, factory: SmtpFactory = smtplib.SMTP) -> None:
    try:
        with factory(smtp.host, smtp.port) as client:
            if smtp.starttls:
                client.starttls()
            if smtp.username:
                client.login(smtp.username, resolve_secret(smtp.password or ""))
            client.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise ModuleError(f"smtp login failed: {exc}", retryable=False) from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise ModuleError(f"smtp: {exc}", retryable=True) from exc


def notify_review(
    config: GeorgConfig,
    sessions: sessionmaker[Session],
    service_id: int,
    *,
    factory: SmtpFactory = smtplib.SMTP,
) -> None:
    if not config.review.notify:
        return
    message = review_notice(config, sessions, service_id)
    assert config.review.smtp is not None
    send(message, config.review.smtp, factory)
