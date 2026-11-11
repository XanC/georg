# SPDX-License-Identifier: AGPL-3.0-only
import re
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from georg.core.config import GeorgConfig
from georg.db.engine import session_factory
from georg.db.repositories import UserRepository
from georg.idle import ActivityMarker, IdleWatchdog, idle_state, run_command
from georg.jobs.queue import JobQueue
from georg.pipeline.worker import Worker
from georg.web import create_app
from georg.web.auth import hash_password

START = datetime(2026, 11, 1, 19, 0, tzinfo=UTC)
IDLE = timedelta(minutes=30)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeShutdown:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def marker(tmp_path: Path, clock: Clock) -> ActivityMarker:
    return ActivityMarker.for_data_dir(tmp_path / "data", clock=clock)


@pytest.fixture
def queue(engine: Engine, clock: Clock) -> JobQueue:
    return JobQueue(engine, clock=clock)


def _watchdog(
    engine: Engine, marker: ActivityMarker, clock: Clock, shutdown: FakeShutdown
) -> IdleWatchdog:
    return IdleWatchdog(engine, idle_after=IDLE, marker=marker, shutdown=shutdown, clock=clock)


def test_fires_once_after_idle_period_from_start(
    engine: Engine, marker: ActivityMarker, clock: Clock
) -> None:
    shutdown = FakeShutdown()
    watchdog = _watchdog(engine, marker, clock, shutdown)
    clock.advance(minutes=29)
    assert not watchdog.check()
    clock.advance(minutes=1)
    assert watchdog.check()
    assert watchdog.check()
    assert shutdown.calls == 1


def test_old_activity_marker_does_not_fire_right_after_boot(
    engine: Engine, marker: ActivityMarker, clock: Clock
) -> None:
    marker.touch()
    clock.advance(days=7)
    shutdown = FakeShutdown()
    watchdog = _watchdog(engine, marker, clock, shutdown)
    assert not watchdog.check()
    clock.advance(minutes=30)
    assert watchdog.check()


def test_web_activity_restarts_idle_period(
    engine: Engine, marker: ActivityMarker, clock: Clock
) -> None:
    shutdown = FakeShutdown()
    watchdog = _watchdog(engine, marker, clock, shutdown)
    clock.advance(minutes=20)
    marker.touch()
    clock.advance(minutes=20)
    assert not watchdog.check()
    clock.advance(minutes=10)
    assert watchdog.check()


def test_queued_and_running_jobs_keep_host_awake(
    engine: Engine, marker: ActivityMarker, clock: Clock, queue: JobQueue
) -> None:
    shutdown = FakeShutdown()
    watchdog = _watchdog(engine, marker, clock, shutdown)
    queue.enqueue("process", {"service_id": 1}, run_after=START + timedelta(hours=2))
    clock.advance(hours=3)
    assert not watchdog.check()
    job = queue.lease("w1")
    assert job is not None
    clock.advance(hours=1)
    queue.heartbeat(job.id, "w1")
    assert not watchdog.check(), "must not fire while a job runs"
    queue.complete(job.id, "w1")
    clock.advance(minutes=29)
    assert not watchdog.check(), "a finished job counts as activity"
    clock.advance(minutes=1)
    assert watchdog.check()
    assert shutdown.calls == 1


def test_failed_and_cancelled_jobs_do_not_keep_host_awake(
    engine: Engine, marker: ActivityMarker, clock: Clock, queue: JobQueue
) -> None:
    watchdog = _watchdog(engine, marker, clock, FakeShutdown())
    job_id = queue.enqueue("process", max_attempts=1)
    job = queue.lease("w1")
    assert job is not None and job.id == job_id
    queue.fail(job_id, "w1", RuntimeError("boom"))
    queue.cancel(queue.enqueue("render"))
    clock.advance(minutes=31)
    assert watchdog.check()


def test_failed_shutdown_retries_after_another_idle_period(
    engine: Engine, marker: ActivityMarker, clock: Clock
) -> None:
    shutdown = FakeShutdown(OSError("no permission"))
    watchdog = _watchdog(engine, marker, clock, shutdown)
    clock.advance(minutes=30)
    assert not watchdog.check()
    clock.advance(minutes=29)
    assert not watchdog.check()
    assert shutdown.calls == 1
    shutdown.error = None
    clock.advance(minutes=1)
    assert watchdog.check()
    assert shutdown.calls == 2


def test_watchdogs_in_several_workers_never_fire_during_a_job(
    engine: Engine, marker: ActivityMarker, clock: Clock, queue: JobQueue
) -> None:
    shutdown = FakeShutdown()
    dogs = [_watchdog(engine, marker, clock, shutdown) for _ in range(4)]
    queue.enqueue("process")
    clock.advance(hours=1)
    job = queue.lease("w1")
    assert job is not None
    results: list[bool] = []
    threads = [threading.Thread(target=lambda d=d: results.append(d.check())) for d in dogs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [False] * 4
    assert shutdown.calls == 0


def test_marker_touches_are_throttled(tmp_path: Path, clock: Clock) -> None:
    marker = ActivityMarker(tmp_path / "m", clock=clock, min_interval=timedelta(seconds=30))
    assert marker.last() is None
    marker.touch()
    clock.advance(seconds=10)
    marker.touch()
    assert marker.last() == START
    clock.advance(seconds=20)
    marker.touch()
    assert marker.last() == START + timedelta(seconds=30)


def test_run_command(tmp_path: Path) -> None:
    flag = tmp_path / "poweroff"
    run_command([sys.executable, "-c", f"open({str(flag)!r}, 'w').close()"])()
    assert flag.exists()
    with pytest.raises(Exception, match="exit status 3"):
        run_command([sys.executable, "-c", "raise SystemExit(3)"])()


def _config(tmp_path: Path, **extra: object) -> GeorgConfig:
    return GeorgConfig.model_validate({
        "instance": "test",
        "data_dir": tmp_path / "data",
        "inputs": [{"name": "drive", "type": "local_folder", "options": {"root": tmp_path}}],
        "processors": {k: {"type": "unused"} for k in
                       ("classifier", "detector", "transcriber", "formatter", "metadata")},
        **extra,
    })  # fmt: skip


def test_config_defaults_off(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.idle_shutdown_minutes is None
    assert config.idle_shutdown_command == ("sudo", "systemctl", "poweroff")


def test_web_records_activity_except_healthz(engine: Engine, tmp_path: Path, clock: Clock) -> None:
    marker = ActivityMarker.for_data_dir(tmp_path / "data", clock=clock)
    app = create_app(_config(tmp_path), engine, secret_key="k" * 32, activity=marker)
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert marker.last() is None
    client.get("/login")
    assert marker.last() == START


def test_web_enables_marker_when_watchdog_configured(engine: Engine, tmp_path: Path) -> None:
    config = _config(tmp_path, idle_shutdown_minutes=30)
    client = TestClient(create_app(config, engine, secret_key="k" * 32))
    client.get("/login")
    assert ActivityMarker.for_data_dir(config.data_dir).last() is not None


def test_worker_loop_checks_watchdog_when_idle(
    engine: Engine, marker: ActivityMarker, clock: Clock, tmp_path: Path
) -> None:
    stop = threading.Event()

    def shutdown() -> None:
        stop.set()

    watchdog = IdleWatchdog(engine, idle_after=IDLE, marker=marker, shutdown=shutdown, clock=clock)
    clock.advance(hours=1)
    worker = Worker(_config(tmp_path), engine, watchdog=watchdog, worker_id="w1")
    worker.run(poll_seconds=0.01, stop=stop)
    assert watchdog.fired


def _status_client(
    engine: Engine, tmp_path: Path, clock: Clock, **extra: object
) -> tuple[TestClient, ActivityMarker, str]:
    with session_factory(engine).begin() as s:
        UserRepository(s).add("rev", hash_password("pw-123456"), "reviewer")
    marker = ActivityMarker.for_data_dir(tmp_path / "data", clock=clock)
    config = _config(tmp_path, **extra)
    client = TestClient(create_app(config, engine, secret_key="k" * 32, activity=marker))
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)
    assert match
    data = {"username": "rev", "password": "pw-123456", "csrf_token": match.group(1)}
    assert client.post("/login", data=data, follow_redirects=False).status_code == 303
    match = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text)
    assert match
    return client, marker, match.group(1)


def test_idle_status_does_not_count_as_activity(
    engine: Engine, tmp_path: Path, clock: Clock
) -> None:
    client, marker, _ = _status_client(engine, tmp_path, clock, idle_shutdown_minutes=30)
    clock.advance(minutes=5)
    response = client.get("/idle-status")
    assert response.json() == {"enabled": True, "busy": False, "seconds_left": 25 * 60}
    assert marker.last() == START
    clock.advance(minutes=30)
    assert client.get("/idle-status").json()["seconds_left"] == 0


def test_keepalive_counts_even_within_throttle(
    engine: Engine, tmp_path: Path, clock: Clock
) -> None:
    client, marker, token = _status_client(engine, tmp_path, clock, idle_shutdown_minutes=30)
    clock.advance(seconds=10)
    response = client.post("/keepalive", headers={"X-CSRF-Token": token})
    assert response.status_code == 204
    assert marker.last() == START + timedelta(seconds=10)
    clock.advance(minutes=1)
    assert client.get("/idle-status").json()["seconds_left"] == 29 * 60


def test_keepalive_requires_csrf_and_login(engine: Engine, tmp_path: Path, clock: Clock) -> None:
    client, marker, _ = _status_client(engine, tmp_path, clock, idle_shutdown_minutes=30)
    clock.advance(seconds=10)
    assert client.post("/keepalive").status_code == 403
    assert client.post("/keepalive", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert marker.last() == START
    anonymous = TestClient(client.app)
    assert anonymous.get("/idle-status", follow_redirects=False).status_code == 303
    assert anonymous.post("/keepalive", follow_redirects=False).status_code == 403


def test_idle_status_reports_busy_and_last_job_finish(
    engine: Engine, tmp_path: Path, clock: Clock, queue: JobQueue
) -> None:
    client, _, _ = _status_client(engine, tmp_path, clock, idle_shutdown_minutes=30)
    queue.enqueue("process")
    assert client.get("/idle-status").json()["busy"] is True
    job = queue.lease("w1")
    assert job is not None
    assert client.get("/idle-status").json()["busy"] is True
    clock.advance(minutes=20)
    queue.complete(job.id, "w1")
    clock.advance(minutes=5)
    status = client.get("/idle-status").json()
    assert status == {"enabled": True, "busy": False, "seconds_left": 25 * 60}


def test_idle_status_counts_from_process_start(
    engine: Engine, tmp_path: Path, clock: Clock
) -> None:
    marker = ActivityMarker.for_data_dir(tmp_path / "data", clock=clock)
    marker.touch()
    clock.advance(hours=2)
    with session_factory(engine).begin() as s:
        state = idle_state(s.connection(), marker, clock())
    assert state.last_activity == clock()
    assert state.remaining(IDLE, clock() + timedelta(minutes=1)) == IDLE - timedelta(minutes=1)


def test_watchdog_start_and_failed_shutdown_touch_marker(
    engine: Engine, marker: ActivityMarker, clock: Clock
) -> None:
    watchdog = _watchdog(engine, marker, clock, FakeShutdown(RuntimeError("no")))
    assert marker.last() == START
    clock.advance(minutes=31)
    assert not watchdog.check()
    assert marker.last() == clock()


def test_idle_status_disabled(engine: Engine, tmp_path: Path, clock: Clock) -> None:
    client, _, token = _status_client(engine, tmp_path, clock)
    assert client.get("/idle-status").json() == {
        "enabled": False,
        "busy": False,
        "seconds_left": None,
    }
    assert client.post("/keepalive", headers={"X-CSRF-Token": token}).status_code == 204
    assert 'id="idle-banner"' in client.get("/").text
