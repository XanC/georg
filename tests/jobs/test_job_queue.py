# SPDX-License-Identifier: AGPL-3.0-only
import multiprocessing
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from georg.core.errors import ModuleError, SourceNotReadyError
from georg.db import create_engine
from georg.jobs import JobQueue, LeaseLostError

WORKERS = 4
START = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def queue(engine: Engine, clock: Clock) -> JobQueue:
    return JobQueue(
        engine,
        lease_duration=timedelta(minutes=5),
        backoff_base=timedelta(seconds=30),
        backoff_max=timedelta(minutes=10),
        max_attempts=3,
        clock=clock,
    )


def test_enqueue_lease_complete(queue: JobQueue) -> None:
    job_id = queue.enqueue("transcribe", {"service_id": 7})
    job = queue.lease("w1")
    assert job is not None
    assert (job.id, job.kind, job.payload, job.attempts) == (
        job_id,
        "transcribe",
        {"service_id": 7},
        1,
    )
    assert job.lease_until == START + timedelta(minutes=5)
    assert queue.lease("w2") is None
    queue.complete(job.id, "w1")
    info = queue.get(job_id)
    assert (info.status, info.worker_id, info.lease_until, info.finished_at) == (
        "succeeded",
        "w1",
        None,
        START,
    )


def test_jobs_wait_for_run_after(queue: JobQueue, clock: Clock) -> None:
    later = queue.enqueue("b", run_after=START + timedelta(minutes=1))
    sooner = queue.enqueue("a")
    job = queue.lease("w")
    assert job is not None and job.id == sooner
    assert queue.lease("w") is None
    clock.advance(minutes=1)
    job = queue.lease("w")
    assert job is not None and job.id == later


def test_lease_filters_by_kind(queue: JobQueue) -> None:
    queue.enqueue("publish")
    assert queue.lease("w", kinds=["classify"]) is None
    assert queue.lease("w", kinds=[]) is None
    job = queue.lease("w", kinds=["classify", "publish"])
    assert job is not None and job.kind == "publish"


def test_retryable_failure_backs_off_exponentially(queue: JobQueue, clock: Clock) -> None:
    job_id = queue.enqueue("transcribe")
    for attempt, delay in ((1, 30), (2, 60)):
        job = queue.lease("w")
        assert job is not None and job.attempts == attempt
        assert queue.fail(job_id, "w", ModuleError("timeout", retryable=True)) == "queued"
        info = queue.get(job_id)
        assert info.run_after == clock.now + timedelta(seconds=delay)
        assert info.last_error == "ModuleError: timeout"
        assert info.worker_id is None
        clock.advance(seconds=delay - 1)
        assert queue.lease("w") is None
        clock.advance(seconds=1)
    job = queue.lease("w")
    assert job is not None and job.attempts == 3
    assert queue.fail(job_id, "w", SourceNotReadyError("still uploading")) == "failed"
    info = queue.get(job_id)
    assert (info.status, info.attempts, info.finished_at) == ("failed", 3, clock.now)


def test_non_retryable_failures_fail_at_once(queue: JobQueue) -> None:
    for error in (ModuleError("bad input"), ValueError("bug"), "plain message"):
        job_id = queue.enqueue("x")
        assert queue.lease("w") is not None
        assert queue.fail(job_id, "w", error) == "failed"
    assert [j.status for j in queue.list()] == ["failed"] * 3
    assert queue.list(status="failed")[1].last_error == "ValueError: bug"
    job_id = queue.enqueue("x")
    assert queue.lease("w") is not None
    assert queue.fail(job_id, "w", "flaky", retryable=True) == "queued"


def test_backoff_is_capped(queue: JobQueue) -> None:
    assert [queue.backoff(n).total_seconds() for n in range(1, 7)] == [30, 60, 120, 240, 480, 600]
    assert queue.backoff(1000) == timedelta(minutes=10)


def test_heartbeat_extends_lease(queue: JobQueue, clock: Clock) -> None:
    job_id = queue.enqueue("classify")
    assert queue.lease("w1") is not None
    clock.advance(minutes=4)
    assert queue.heartbeat(job_id, "w1") == clock.now + timedelta(minutes=5)
    clock.advance(minutes=4)
    assert queue.reclaim_stale() == 0
    assert queue.lease("w2") is None
    with pytest.raises(LeaseLostError):
        queue.heartbeat(job_id, "w2")


def test_stale_lease_is_reclaimed(queue: JobQueue, clock: Clock) -> None:
    job_id = queue.enqueue("classify")
    assert queue.lease("crashed") is not None
    clock.advance(minutes=5, seconds=1)
    job = queue.lease("w2")
    assert job is not None and job.id == job_id and job.attempts == 2
    assert queue.get(job_id).worker_id == "w2"
    with pytest.raises(LeaseLostError):
        queue.complete(job_id, "crashed")
    with pytest.raises(LeaseLostError):
        queue.fail(job_id, "crashed", "late")
    queue.complete(job_id, "w2")
    assert queue.get(job_id).status == "succeeded"


def test_stale_lease_on_last_attempt_fails(queue: JobQueue, clock: Clock) -> None:
    job_id = queue.enqueue("classify", max_attempts=1)
    assert queue.lease("crashed") is not None
    clock.advance(minutes=6)
    assert queue.reclaim_stale() == 1
    info = queue.get(job_id)
    assert info.status == "failed"
    assert info.last_error == "lease expired for worker crashed"


def test_cancel_only_queued(queue: JobQueue) -> None:
    first = queue.enqueue("a")
    second = queue.enqueue("b")
    assert queue.lease("w") is not None
    assert not queue.cancel(first)
    assert queue.cancel(second)
    assert queue.get(second).status == "cancelled"
    assert queue.lease("w") is None


def _drain(db_path: str, worker_id: str, start: "threading.Barrier") -> list[int]:
    engine = create_engine(Path(db_path))
    queue = JobQueue(engine)
    start.wait(timeout=60)
    leased: list[int] = []
    while (job := queue.lease(worker_id)) is not None:
        leased.append(job.id)
        queue.complete(job.id, worker_id)
    engine.dispose()
    return leased


def test_concurrent_workers_never_share_a_job(db_path: Path, engine: Engine) -> None:
    queue = JobQueue(engine)
    ids = {queue.enqueue("work", {"n": n}) for n in range(400)}
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager, ctx.Pool(WORKERS) as pool:
        start = manager.Barrier(WORKERS)
        results = [
            pool.apply_async(_drain, (str(db_path), f"worker-{i}", start)) for i in range(WORKERS)
        ]
        leased = [result.get(timeout=120) for result in results]
    flat = [job_id for worker in leased for job_id in worker]
    assert len(flat) == len(set(flat)) == len(ids)
    assert set(flat) == ids
    assert all(j.status == "succeeded" and j.attempts == 1 for j in queue.list())
    assert sum(1 for worker in leased if worker) >= 2
