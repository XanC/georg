# SPDX-License-Identifier: AGPL-3.0-only
"""Email notices to reviewers."""

import logging
import os
import re
import smtplib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session, sessionmaker

from georg.core.config import SECRET_PREFIX, GeorgConfig, SmtpConfig, resolve_secret
from georg.core.errors import ConfigError, ModuleError
from georg.db.repositories import PublicationRepository, SermonRepository, ServiceRepository
from georg.jobs.queue import JobInfo

log = logging.getLogger(__name__)

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
                client.login(resolve_secret(smtp.username), resolve_secret(smtp.password or ""))
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


def notify_job_failed(
    config: GeorgConfig,
    sessions: sessionmaker[Session],
    job: JobInfo,
    *,
    factory: SmtpFactory = smtplib.SMTP,
) -> None:
    """Email reviewers that `job` failed permanently; do nothing if email is not set up."""
    review = config.review
    if not review.notify or review.smtp is None:
        return
    send(failure_notice(config, sessions, job), review.smtp, factory)


@dataclass
class _JobContext:
    service_id: int | None = None
    sermon_id: int | None = None
    label: str | None = None
    day: date | None = None
    outputs: list[str] = field(default_factory=list)


def failure_notice(
    config: GeorgConfig, sessions: sessionmaker[Session], job: JobInfo
) -> EmailMessage:
    """The email telling reviewers that `job` failed and will not be retried."""
    review = config.review
    if review.smtp is None:
        raise ConfigError("review.notify is set but review.smtp is not configured")
    try:
        ctx = _job_context(config, sessions, job)
    except Exception:
        log.exception("job %d: cannot look up the service for the failure notice", job.id)
        ctx = _JobContext()
    if ctx.service_id is None:
        ctx.service_id = _int(job.payload.get("service_id"))
    secrets = list(_secret_values(config))
    when = ctx.day.isoformat() if ctx.day else None
    subject = f"Georg: {job.kind} failed"
    if when:
        subject += f" for {when}"
    elif ctx.service_id is not None:
        subject += f" for service {ctx.service_id}"
    lines = [
        f"A {job.kind} job failed and will not be retried. Someone needs to look at it.",
        "",
        f"Error: {redact(job.last_error or 'unknown error', secrets)}",
    ]
    if ctx.service_id is not None:
        name = ", ".join(x for x in (ctx.label, when or "undated") if x)
        lines.append(f"Service: {name} (service {ctx.service_id})")
    if ctx.outputs:
        lines += ["Outputs:", *(f"  {redact(o, secrets)}" for o in ctx.outputs)]
    lines += [
        f"Attempts: {job.attempts} of {job.max_attempts}",
        f"Job: {job.id}",
    ]
    if review.base_url:
        base = review.base_url.rstrip("/")
        link = f"{base}/services/{ctx.service_id}" if ctx.service_id else f"{base}/jobs"
        lines += ["", f"Review: {link}"]
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = review.smtp.sender
    message["To"] = ", ".join(review.notify)
    message.set_content("\n".join(lines) + "\n")
    return message


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _job_context(config: GeorgConfig, sessions: sessionmaker[Session], job: JobInfo) -> _JobContext:
    ctx = _JobContext()
    payload = job.payload
    with sessions() as session:
        sermons = SermonRepository(session)
        if (revision_id := _int(payload.get("revision_id"))) is not None:
            ctx.sermon_id = sermons.revision(revision_id).sermon_id
        else:
            ctx.sermon_id = _int(payload.get("sermon_id"))
        if ctx.sermon_id is not None:
            ctx.service_id = sermons.get(ctx.sermon_id).service_id
        else:
            ctx.service_id = _int(payload.get("service_id"))
        if ctx.service_id is None:
            return ctx
        service = ServiceRepository(session).get(ctx.service_id)
        ctx.label, ctx.day = service.title, service.service_date
        found = sermons.for_service(ctx.service_id)
        if ctx.sermon_id is None and found:
            ctx.sermon_id = found[0].id
        revision = sermons.current_revision(ctx.sermon_id) if ctx.sermon_id else None
        if revision is not None:
            meta = sermons.metadata(revision)
            ctx.label = meta.title or meta.occasion or ctx.label
            ctx.day = meta.service_date or ctx.day
        if payload.get("sermon_id") is not None and ctx.sermon_id is not None:
            pubs = {
                p.target_name: p for p in PublicationRepository(session).for_sermon(ctx.sermon_id)
            }
            for output in config.outputs:
                pub = pubs.get(output.name)
                if pub is None:
                    ctx.outputs.append(f"{output.name}: not published")
                elif pub.status == "failed" and pub.last_error:
                    ctx.outputs.append(f"{output.name}: failed: {pub.last_error}")
                else:
                    ctx.outputs.append(f"{output.name}: {pub.status}")
    return ctx


_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (["']?\b[\w-]*(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|signature|
    credential|authorization|key)["']?\s*[:=]\s*["']?)
    [^\s"',&;}]+
    """
)
REDACTED = "[redacted]"


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove credentials an error message might carry.

    Drops URL user info, query strings and fragments, bearer tokens, `key=value`
    pairs whose key looks secret, and any of the given secret values.
    """
    for secret in sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _URL.sub(_redact_url, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _ASSIGNMENT.sub(lambda m: m.group(1) + REDACTED, text)


def _redact_url(match: re.Match[str]) -> str:
    url = match.group(0)
    trailing = ""
    while url and url[-1] in ".,;:)]}":
        url, trailing = url[:-1], url[-1] + trailing
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port is not None:
            host += f":{parts.port}"
    except ValueError:
        return REDACTED + trailing
    query = REDACTED if parts.query else ""
    return urlunsplit((parts.scheme, host, parts.path, query, "")) + trailing


def _secret_values(config: GeorgConfig) -> Iterator[str]:
    """Values of every "env:NAME" reference in the configuration."""

    def walk(value: object) -> Iterator[str]:
        if isinstance(value, str) and value.startswith(SECRET_PREFIX):
            resolved = os.environ.get(value.removeprefix(SECRET_PREFIX))
            if resolved:
                yield resolved
        elif isinstance(value, dict):
            for v in value.values():
                yield from walk(v)
        elif isinstance(value, list | tuple):
            for v in value:
                yield from walk(v)

    yield from walk(config.model_dump())
