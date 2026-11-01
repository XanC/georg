# SPDX-License-Identifier: AGPL-3.0-only
"""The worker role: leases jobs from the queue and runs them."""

import logging
import os
import socket
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine

from georg.core.config import GeorgConfig
from georg.db.engine import session_factory
from georg.jobs.queue import JobQueue, LeasedJob, LeaseLostError
from georg.pipeline.registry import Processors, build_processors
from georg.pipeline.service import DISCOVER, PROCESS, discover, process_service

log = logging.getLogger(__name__)


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
    def __init__(
        self,
        config: GeorgConfig,
        engine: Engine,
        *,
        queue: JobQueue | None = None,
        processors: Processors | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.config = config
        self.sessions = session_factory(engine)
        self.queue = queue or JobQueue(engine)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._processors = processors
        self.handlers: dict[str, Callable[[dict[str, Any]], object]] = {
            DISCOVER: lambda _: discover(self.config, self.sessions, self.queue),
            PROCESS: lambda payload: process_service(
                self.config, self.sessions, int(payload["service_id"]), self.processors
            ),
        }

    @property
    def processors(self) -> Processors:
        if self._processors is None:
            self._processors = build_processors(self.config.processors)
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
        try:
            self.queue.complete(job.id, self.worker_id)
        except LeaseLostError:
            log.warning("job %d: lease lost before completion; it will run again", job.id)
        return True

    def run(self, *, poll_seconds: float = 30.0, stop: threading.Event | None = None) -> None:
        """Run jobs until `stop` is set, sleeping `poll_seconds` when idle."""
        stop = stop or threading.Event()
        while not stop.is_set():
            self.queue.reclaim_stale()
            if not self.run_once():
                stop.wait(poll_seconds)
