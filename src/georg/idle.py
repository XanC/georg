# SPDX-License-Identifier: AGPL-3.0-only
"""Idle watchdog: power the host off when Georg has had nothing to do for a while.

Activity is shared between processes through the database and one marker file:

- the web process touches `<data_dir>/run/web-activity` on every request except
  `/healthz` (see `ActivityMarker`);
- queued or running jobs keep the host awake, and a job finishing counts as
  activity, so the idle period restarts after the last job.

The job check and the shutdown command run inside one `BEGIN IMMEDIATE`
transaction, so no worker in any process can lease a job between the check and
the shutdown request. Any number of worker processes may run a watchdog.
"""

import logging
import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import Engine, Table, func, select

from georg.db.engine import writer
from georg.db.models import Job
from georg.db.types import utcnow

log = logging.getLogger(__name__)

_jobs = cast(Table, Job.__table__).c
EXEMPT_PATHS = frozenset({"/healthz"})
"""Requests that do not count as activity (the wake page polls `/healthz`)."""


class ActivityMarker:
    """A file whose modification time is the last web request."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = utcnow,
        min_interval: timedelta = timedelta(seconds=30),
    ) -> None:
        self.path = path
        self._clock = clock
        self._min_interval = min_interval
        self._last_touch: datetime | None = None
        self._lock = threading.Lock()

    @classmethod
    def for_data_dir(
        cls, data_dir: Path, *, clock: Callable[[], datetime] = utcnow
    ) -> "ActivityMarker":
        return cls(data_dir / "run" / "web-activity", clock=clock)

    def touch(self) -> None:
        now = self._clock()
        with self._lock:
            if self._last_touch is not None and now - self._last_touch < self._min_interval:
                return
            self._last_touch = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        stamp = now.timestamp()
        os.utime(self.path, (stamp, stamp))

    def last(self) -> datetime | None:
        try:
            return datetime.fromtimestamp(self.path.stat().st_mtime, UTC)
        except FileNotFoundError:
            return None


def run_command(command: Sequence[str]) -> Callable[[], None]:
    """A shutdown action that runs `command` and raises if it fails."""

    def shutdown() -> None:
        subprocess.run(list(command), check=True, timeout=60)

    return shutdown


class IdleWatchdog:
    """Calls `shutdown` once nothing has happened for `idle_after`."""

    def __init__(
        self,
        engine: Engine,
        *,
        idle_after: timedelta,
        marker: ActivityMarker,
        shutdown: Callable[[], None],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if idle_after <= timedelta(0):
            raise ValueError("idle_after must be positive")
        self._engine = writer(engine)
        self.idle_after = idle_after
        self.marker = marker
        self._shutdown = shutdown
        self._clock = clock
        self._armed_at = clock()
        """Start of the grace period: process start, or the last failed shutdown."""
        self.fired = False

    def check(self) -> bool:
        """Request shutdown if idle; return True if it did (now or earlier)."""
        if self.fired:
            return True
        now = self._clock()
        if self._last_web_or_start() > now - self.idle_after:
            return False
        with self._engine.begin() as conn:
            busy, last_finished = conn.execute(
                select(
                    func.count().filter(_jobs.status.in_(("queued", "running"))),
                    func.max(_jobs.finished_at),
                )
            ).one()
            if busy or (last_finished is not None and last_finished > now - self.idle_after):
                return False
            last = max(self._last_web_or_start(), last_finished or self._armed_at)
            log.warning("idle since %s with no pending jobs; shutting down", last.isoformat())
            try:
                self._shutdown()
            except Exception:
                log.exception("shutdown command failed; retrying after another idle period")
                self._armed_at = now
                return False
        self.fired = True
        return True

    def _last_web_or_start(self) -> datetime:
        last_web = self.marker.last()
        return self._armed_at if last_web is None else max(self._armed_at, last_web)
