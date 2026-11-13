# SPDX-License-Identifier: AGPL-3.0-only
"""The `jobs` table as a queue.

A worker leases a job for a limited time and must heartbeat to keep it. Each
operation is one `BEGIN IMMEDIATE` transaction, and leasing is a single
`UPDATE ... RETURNING`, so concurrent workers in separate processes never receive
the same job. Leases that run out (a crashed worker) are reclaimed before leasing.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Connection, Engine, Row, Table, func, insert, literal, select, update

from georg.core.errors import GeorgError, ModuleError
from georg.db.engine import writer
from georg.db.models import Job
from georg.db.types import utcnow

_table = cast(Table, Job.__table__)
_jobs = _table.c


class LeaseLostError(GeorgError):
    """The worker no longer holds the job's lease (it expired and was reclaimed)."""


@dataclass(frozen=True)
class LeasedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    """Including the current attempt."""
    max_attempts: int
    worker_id: str
    lease_until: datetime


@dataclass(frozen=True)
class JobInfo:
    id: int
    kind: str
    payload: dict[str, Any]
    status: str
    run_after: datetime
    lease_until: datetime | None
    worker_id: str | None
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None
    failure_notified_at: datetime | None = None


class JobQueue:
    def __init__(
        self,
        engine: Engine,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
        backoff_base: timedelta = timedelta(seconds=30),
        backoff_max: timedelta = timedelta(hours=1),
        max_attempts: int = 5,
        retry_deadline: timedelta = timedelta(hours=48),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._engine = writer(engine)
        self.lease_duration = lease_duration
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_attempts = max_attempts
        self.retry_deadline = retry_deadline
        self._clock = clock

    def enqueue(
        self,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        run_after: datetime | None = None,
        max_attempts: int | None = None,
    ) -> int:
        now = self._clock()
        stmt = (
            insert(_table)
            .values(
                kind=kind,
                payload_json=dict(payload or {}),
                status="queued",
                run_after=run_after or now,
                attempts=0,
                max_attempts=max_attempts or self.max_attempts,
                created_at=now,
            )
            .returning(_jobs.id)
        )
        with self._engine.begin() as conn:
            job_id: int = conn.execute(stmt).scalar_one()
        return job_id

    def lease(self, worker_id: str, *, kinds: Sequence[str] | None = None) -> LeasedJob | None:
        """Claim the next due job, or return None if there is none."""
        if kinds is not None and not kinds:
            return None
        now = self._clock()
        lease_until = now + self.lease_duration
        candidate = (
            select(_jobs.id)
            .where(_jobs.status == "queued", _jobs.run_after <= now)
            .order_by(_jobs.run_after, _jobs.id)
            .limit(1)
        )
        if kinds is not None:
            candidate = candidate.where(_jobs.kind.in_(kinds))
        stmt = (
            update(_table)
            .where(_jobs.id == candidate.scalar_subquery())
            .values(
                status="running",
                worker_id=worker_id,
                lease_until=lease_until,
                attempts=_jobs.attempts + 1,
            )
            .returning(_jobs.id, _jobs.kind, _jobs.payload_json, _jobs.attempts, _jobs.max_attempts)
        )
        with self._engine.begin() as conn:
            self._reclaim(conn, now)
            row = conn.execute(stmt).one_or_none()
        if row is None:
            return None
        return LeasedJob(
            id=row.id,
            kind=row.kind,
            payload=row.payload_json,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            worker_id=worker_id,
            lease_until=lease_until,
        )

    def heartbeat(self, job_id: int, worker_id: str) -> datetime:
        """Extend the lease; return the new expiry."""
        lease_until = self._clock() + self.lease_duration
        with self._engine.begin() as conn:
            self._update_owned(conn, job_id, worker_id, lease_until=lease_until)
        return lease_until

    def complete(self, job_id: int, worker_id: str) -> None:
        with self._engine.begin() as conn:
            self._update_owned(
                conn,
                job_id,
                worker_id,
                status="succeeded",
                lease_until=None,
                last_error=None,
                finished_at=self._clock(),
            )

    def fail(
        self,
        job_id: int,
        worker_id: str,
        error: BaseException | str,
        *,
        retryable: bool | None = None,
    ) -> str:
        """Record a failed attempt; return the job's new status.

        The job is retried after an exponential backoff if it is retryable and has
        attempts left. Unless `retryable` is given, only a `ModuleError` with
        `retryable=True` is retried; other exceptions are treated as bugs.

        A `ModuleError` carrying `retry_after` waits at least that long, and keeps
        the job retrying past `max_attempts` while the next run would still land
        within `retry_deadline` of the job's creation (long server-side waits,
        like a daily download quota, should not burn the normal attempt budget).
        """
        if retryable is None:
            retryable = isinstance(error, ModuleError) and error.retryable
        retry_after = error.retry_after if isinstance(error, ModuleError) else None
        message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        now = self._clock()
        with self._engine.begin() as conn:
            row = conn.execute(
                select(_jobs.attempts, _jobs.max_attempts, _jobs.created_at).where(
                    *self._owned(job_id, worker_id)
                )
            ).one_or_none()
            if row is None:
                raise LeaseLostError(f"job {job_id} is not leased by {worker_id}")
            delay = self.backoff(row.attempts)
            if retry_after is not None and retry_after > delay:
                delay = retry_after
            if retryable and (
                row.attempts < row.max_attempts
                or (retry_after is not None and now + delay <= row.created_at + self.retry_deadline)
            ):
                self._update_owned(
                    conn,
                    job_id,
                    worker_id,
                    status="queued",
                    run_after=now + delay,
                    worker_id=None,
                    lease_until=None,
                    last_error=message,
                )
                return "queued"
            self._update_owned(
                conn,
                job_id,
                worker_id,
                status="failed",
                lease_until=None,
                last_error=message,
                finished_at=now,
            )
            return "failed"

    def cancel(self, job_id: int) -> bool:
        """Cancel a job that is waiting to run; return False if it is not queued."""
        stmt = (
            update(_table)
            .where(_jobs.id == job_id, _jobs.status == "queued")
            .values(status="cancelled", finished_at=self._clock())
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def claim_failure_notices(self) -> list[JobInfo]:
        """Mark failed jobs nobody has been told about as notified, and return them.

        Claiming before the email is sent means each failure is reported at most once,
        even with several workers or after a restart.
        """
        stmt = (
            update(_table)
            .where(_jobs.status == "failed", _jobs.failure_notified_at.is_(None))
            .values(failure_notified_at=self._clock())
            .returning(*_table.c)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).all()
        return sorted((_info(row) for row in rows), key=lambda job: job.id)

    def reclaim_stale(self) -> int:
        """Requeue (or fail, if out of attempts) jobs whose lease has expired."""
        with self._engine.begin() as conn:
            return self._reclaim(conn, self._clock())

    def backoff(self, attempts: int) -> timedelta:
        """Delay before retrying after the `attempts`-th failed attempt."""
        exponent = max(attempts - 1, 0)
        if exponent >= 32:
            return self.backoff_max
        return min(self.backoff_base * (1 << exponent), self.backoff_max)

    def counts(self) -> dict[str, int]:
        """Number of jobs per status."""
        stmt = select(_jobs.status, func.count()).group_by(_jobs.status)
        with self._engine.connect() as conn:
            return {status: int(n) for status, n in conn.execute(stmt)}

    def failed_since(self, since: datetime) -> list[JobInfo]:
        stmt = (
            select(_table)
            .where(_jobs.status == "failed", _jobs.finished_at >= since)
            .order_by(_jobs.finished_at.desc())
        )
        with self._engine.connect() as conn:
            return [_info(row) for row in conn.execute(stmt)]

    def last_finished_at(self) -> datetime | None:
        """When a worker last finished a job (succeeded or failed)."""
        stmt = select(func.max(_jobs.finished_at)).where(_jobs.status.in_(("succeeded", "failed")))
        with self._engine.connect() as conn:
            value: datetime | None = conn.execute(stmt).scalar()
        return value

    def last_created_at(self, kind: str) -> datetime | None:
        """When the newest job of `kind` was queued, whatever its status."""
        stmt = select(func.max(_jobs.created_at)).where(_jobs.kind == kind)
        with self._engine.connect() as conn:
            value: datetime | None = conn.execute(stmt).scalar()
        return value

    def get(self, job_id: int) -> JobInfo:
        with self._engine.connect() as conn:
            row = conn.execute(select(_table).where(_jobs.id == job_id)).one_or_none()
        if row is None:
            raise LookupError(f"job {job_id} not found")
        return _info(row)

    def list(self, *, status: str | None = None) -> list[JobInfo]:
        stmt = select(_table).order_by(_jobs.id)
        if status is not None:
            stmt = stmt.where(_jobs.status == status)
        with self._engine.connect() as conn:
            return [_info(row) for row in conn.execute(stmt)]

    @staticmethod
    def _owned(job_id: int, worker_id: str) -> tuple[Any, ...]:
        return (_jobs.id == job_id, _jobs.worker_id == worker_id, _jobs.status == "running")

    def _update_owned(self, conn: Connection, job_id: int, owner: str, **values: Any) -> None:
        stmt = update(_table).where(*self._owned(job_id, owner)).values(**values)
        if conn.execute(stmt).rowcount != 1:
            raise LeaseLostError(f"job {job_id} is not leased by {owner}")

    @staticmethod
    def _reclaim(conn: Connection, now: datetime) -> int:
        expired = (_jobs.status == "running", _jobs.lease_until < now)
        message = literal("lease expired for worker ") + _jobs.worker_id
        exhausted = conn.execute(
            update(_table)
            .where(*expired, _jobs.attempts >= _jobs.max_attempts)
            .values(
                status="failed",
                last_error=message,
                worker_id=None,
                lease_until=None,
                finished_at=now,
            )
        ).rowcount
        requeued = conn.execute(
            update(_table)
            .where(*expired, _jobs.attempts < _jobs.max_attempts)
            .values(
                status="queued", last_error=message, worker_id=None, lease_until=None, run_after=now
            )
        ).rowcount
        return int(exhausted) + int(requeued)


def _info(row: Row[Any]) -> JobInfo:
    return JobInfo(
        id=row.id,
        kind=row.kind,
        payload=row.payload_json,
        status=row.status,
        run_after=row.run_after,
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        last_error=row.last_error,
        created_at=row.created_at,
        finished_at=row.finished_at,
        failure_notified_at=row.failure_notified_at,
    )
