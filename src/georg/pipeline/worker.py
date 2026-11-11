# SPDX-License-Identifier: AGPL-3.0-only
"""The worker role: leases jobs from the queue and runs them."""

import logging
import os
import smtplib
import socket
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine

from georg.core.config import GeorgConfig
from georg.db.engine import session_factory
from georg.idle import IdleWatchdog
from georg.jobs.queue import JobQueue, LeasedJob, LeaseLostError
from georg.notify import SmtpFactory, notify_job_failed, notify_review
from georg.pipeline.registry import Processors, build_processors
from georg.pipeline.service import (
    DISCOVER,
    PROCESS,
    PUBLISH,
    RENDER,
    discover,
    process_service,
    publish_sermon,
    render_revision,
)

log = logging.getLogger(__name__)

NOTIFY = "notify"


class _Heartbeat:
    """Extends a job's lease in the background while it runs."""

    def __init__(self, queue: JobQueue, job: LeasedJob) -> None:
        self._queue, self._job = queue, job
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        interval = self._queue.lease_duration.total_seconds() / 3
        while not self._stop.wait(interval):
            try:
                self._queue.heartbeat(self._job.id, self._job.worker_id)
            except LeaseLostError:
                log.warning("job %d: lease lost", self._job.id)
                return

    def __enter__(self) -> None:
        self._thread.start()

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()


class Worker:
    """Runs queued jobs: discover, process, notify, render and publish."""

    def __init__(
        self,
        config: GeorgConfig,
        engine: Engine,
        *,
        queue: JobQueue | None = None,
        processors: Processors | None = None,
        worker_id: str | None = None,
        watchdog: IdleWatchdog | None = None,
        smtp_factory: SmtpFactory = smtplib.SMTP,
    ) -> None:
        self.config = config
        self.sessions = session_factory(engine)
        self.queue = queue or JobQueue(engine)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._processors = processors
        self.watchdog = watchdog
        self.smtp_factory = smtp_factory
        self.handlers: dict[str, Callable[[dict[str, Any]], object]] = {
            DISCOVER: lambda _: discover(self.config, self.sessions, self.queue),
            PROCESS: self._process,
            NOTIFY: lambda payload: notify_review(
                self.config, self.sessions, int(payload["service_id"]), factory=self.smtp_factory
            ),
            RENDER: lambda payload: render_revision(
                self.config, self.sessions, int(payload["revision_id"]), self.processors
            ),
            PUBLISH: lambda payload: publish_sermon(
                self.config, self.sessions, int(payload["sermon_id"])
            ),
        }

    def _process(self, payload: dict[str, Any]) -> int | None:
        service_id = int(payload["service_id"])
        revision_id = process_service(
            self.config,
            self.sessions,
            service_id,
            self.processors,
            reprocess=bool(payload.get("reprocess", False)),
        )
        if revision_id is not None and self.config.review.notify:
            self.queue.enqueue(NOTIFY, {"service_id": service_id})
        return revision_id

    @property
    def processors(self) -> Processors:
        if self._processors is None:
            self._processors = build_processors(
                self.config.processors, calendar=self.config.calendar
            )
        return self._processors

    def run_once(self) -> bool:
        """Run the next due job; return False if there was none."""
        job = self.queue.lease(self.worker_id, kinds=list(self.handlers))
        if job is None:
            return False
        log.info("job %d: %s %s (attempt %d)", job.id, job.kind, job.payload, job.attempts)
        try:
            with _Heartbeat(self.queue, job):
                self.handlers[job.kind](job.payload)
        except Exception as exc:
            log.exception("job %d failed", job.id)
            try:
                status = self.queue.fail(job.id, self.worker_id, exc)
                log.info("job %d: now %s", job.id, status)
            except LeaseLostError:
                log.warning("job %d: lease lost before recording the failure", job.id)
                return True
            if status == "failed":
                self.notify_failures()
            return True
        try:
            self.queue.complete(job.id, self.worker_id)
        except LeaseLostError:
            log.warning("job %d: lease lost before completion; it will run again", job.id)
        return True

    def notify_failures(self) -> None:
        """Email reviewers about failed jobs not yet reported, once per job.

        Covers jobs failed by any worker, including leases that ran out of attempts.
        Without email settings, failures are still marked as reported, so turning email
        on later sends no backlog. Errors are logged; they never change a job's state
        or stop the worker.
        """
        try:
            jobs = self.queue.claim_failure_notices()
        except Exception:
            log.exception("cannot claim failed jobs for notification")
            return
        review = self.config.review
        if not review.notify or review.smtp is None:
            return
        for job in jobs:
            try:
                notify_job_failed(self.config, self.sessions, job, factory=self.smtp_factory)
                log.info("job %d: emailed reviewers about the failure", job.id)
            except Exception:
                log.exception("job %d: could not email reviewers about the failure", job.id)

    def run(self, *, poll_seconds: float = 30.0, stop: threading.Event | None = None) -> None:
        """Run jobs until `stop` is set, sleeping `poll_seconds` when idle.

        Between jobs the idle watchdog, if any, may shut the host down.
        """
        stop = stop or threading.Event()
        while not stop.is_set():
            self.queue.reclaim_stale()
            self.notify_failures()
            if not self.run_once():
                if self.watchdog is not None:
                    self.watchdog.check()
                stop.wait(poll_seconds)
