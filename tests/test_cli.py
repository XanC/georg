# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import io
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from georg import __version__
from georg.cli import main, parse_timestamp
from georg.media import probe


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_succeeds() -> None:
    assert main([]) == 0


@pytest.mark.parametrize(
    ("text", "ms"),
    [("90", 90_000), ("1:30.5", 90_500), ("01:02:03.250", 3_723_250), ("0:00:00", 0)],
)
def test_parse_timestamp(text: str, ms: int) -> None:
    assert parse_timestamp(text) == ms


def test_parse_timestamp_rejects_garbage() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_timestamp("1:2:3:4")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_master_sample_writes_ab_pair(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "service.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.05:duration=8",
         "-f", "lavfi", "-i", "color=size=64x64:rate=10:duration=8",
         "-shortest", "-c:v", "libx264", "-c:a", "aac", str(src)],
        check=True,
    )  # fmt: skip
    out = tmp_path / "ab"
    assert main(["master-sample", str(src), str(out), "--start", "0:01", "--end", "0:06"]) == 0
    printed = capsys.readouterr().out.split()
    assert printed == [str(out / "A_unprocessed.mp3"), str(out / "B_mastered.mp3")]
    for name in printed:
        info = probe(Path(name))
        assert abs(info.duration_ms - 5000) <= 250 and info.channels == 2
    assert sorted(p.name for p in out.iterdir()) == ["A_unprocessed.mp3", "B_mastered.mp3"]


def _config_file(tmp_path: Path, discovery: str = "") -> Path:
    path = tmp_path / "georg.toml"
    root = tmp_path / "drive"
    root.mkdir(exist_ok=True)
    processors = "\n".join(
        f'[processors.{k}]\ntype = "unused"'
        for k in ("classifier", "detector", "transcriber", "formatter", "metadata")
    )
    section = f"\n[discovery]\n{discovery}\n" if discovery else ""
    path.write_text(
        f'instance = "test"\ndata_dir = "{tmp_path / "data"}"\n{section}\n'
        f'[[inputs]]\nname = "drive"\ntype = "local_folder"\n'
        f'[inputs.options]\nroot = "{root}"\n\n{processors}\n'
    )
    return path


def test_discover_since_overrides_config_start_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os
    import time

    from georg.db import create_engine
    from georg.db.engine import database_path, session_factory
    from georg.db.repositories import ServiceRepository

    root = tmp_path / "drive"
    for name in ("SPX 10.25.26", "SPX 11.01.26"):
        folder = root / name
        folder.mkdir(parents=True)
        media = folder / "a.mp4"
        media.write_bytes(b"x")
        old = time.time() - 3600
        os.utime(media, (old, old))
    config = _config_file(tmp_path, discovery="start_date = 2026-11-01")

    assert main(["discover", "--config", str(config)]) == 0
    assert "1 new service(s)" in capsys.readouterr().out

    assert main(["discover", "--config", str(config), "--since", "2026-10-01"]) == 0
    assert "1 new service(s)" in capsys.readouterr().out

    engine = create_engine(database_path(tmp_path / "data"))
    with session_factory(engine)() as s:
        dates = sorted(svc.service_date for svc in ServiceRepository(s).newest_first())
    engine.dispose()
    assert dates == [date(2026, 10, 25), date(2026, 11, 1)]


def test_user_add_stores_an_argon2_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from georg.db import create_engine
    from georg.db.engine import database_path, session_factory
    from georg.db.repositories import UserRepository
    from georg.web.auth import verify_password

    config = _config_file(tmp_path)
    argv = ["user", "add", "alice", "--role", "reviewer", "--config", str(config)]
    monkeypatch.setattr("sys.stdin", io.StringIO("long enough password\n"))
    assert main([*argv, "--password-stdin"]) == 0
    assert "added reviewer 'alice'" in capsys.readouterr().out

    engine = create_engine(database_path(tmp_path / "data"))
    with session_factory(engine)() as s:
        user = UserRepository(s).by_username("alice")
        assert user is not None
        assert user.role == "reviewer"
        assert user.password_hash.startswith("$argon2id$")
        assert verify_password(user.password_hash, "long enough password")
    engine.dispose()

    monkeypatch.setattr("sys.stdin", io.StringIO("long enough password\n"))
    assert main([*argv, "--password-stdin"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_user_add_rejects_short_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config_file(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    argv = ["user", "add", "bob", "--role", "admin", "--config", str(config), "--password-stdin"]
    assert main(argv) == 1
    assert "at least 8 characters" in capsys.readouterr().err


def test_web_refuses_to_start_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GEORG_SECRET_KEY", raising=False)
    assert main(["web", "--config", str(_config_file(tmp_path))]) == 1
    assert "GEORG_SECRET_KEY" in capsys.readouterr().err


def test_web_dev_runs_without_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    monkeypatch.delenv("GEORG_SECRET_KEY", raising=False)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    config = str(_config_file(tmp_path))
    assert main(["web", "--config", config, "--dev", "--port", "8123"]) == 0
    assert calls == [{"host": "127.0.0.1", "port": 8123, "proxy_headers": True}]


def test_drive_login_rejects_a_non_desktop_client(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = tmp_path / "client.json"
    client.write_text('{"web": {"client_id": "a", "client_secret": "b"}}')
    out = tmp_path / "creds.json"
    assert main(["drive-login", "--client", str(client), "--out", str(out)]) == 1
    assert "Desktop app" in capsys.readouterr().err
    assert not out.exists()


def test_reprocess_enqueues_a_process_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from georg.core.types import SourceItem, SourcePart
    from georg.db import create_engine, upgrade
    from georg.db.engine import database_path, session_factory
    from georg.db.repositories import ServiceRepository
    from georg.jobs.queue import JobQueue

    config = _config_file(tmp_path)
    db_path = database_path(tmp_path / "data")
    upgrade(db_path)
    engine = create_engine(db_path)
    with session_factory(engine).begin() as s:
        service, _ = ServiceRepository(s).add_discovered(
            SourceItem(
                source_type="local_folder",
                source_ref="SPX 11.01.26",
                parts=(SourcePart(ref="1.wav", name="1.wav", size=1, checksum="x"),),
            )
        )
        service_id = service.id

    assert main(["reprocess", str(service_id), "--config", str(config)]) == 0
    assert f"queued reprocess job 1 for service {service_id}" in capsys.readouterr().out
    [job] = JobQueue(engine).list()
    assert job.kind == "process"
    assert job.payload == {"service_id": service_id, "reprocess": True}

    assert main(["reprocess", "999", "--config", str(config)]) == 1
    assert "not found" in capsys.readouterr().err
    engine.dispose()


def test_cleanup_dry_run_with_days_while_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config_file(tmp_path)
    config.write_text(config.read_text() + "\n[retention]\ndays = 0\n")
    assert main(["cleanup", "--dry-run", "--config", str(config)]) == 0
    assert "pass --days N" in capsys.readouterr().out
    assert main(["cleanup", "--dry-run", "--days", "60", "--config", str(config)]) == 0
    assert "0 service(s), 0 bytes would be freed (dry run)" in capsys.readouterr().out
