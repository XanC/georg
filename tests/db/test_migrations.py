# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from georg.cli import main
from georg.core.errors import GeorgError
from georg.db import create_engine, upgrade
from georg.db.migrate import (
    alembic_config,
    backup_database,
    current_revision,
    head_revision,
    prune_backups,
)
from georg.db.models import Base

TABLES = {
    "users",
    "services",
    "source_parts",
    "artifacts",
    "stage_runs",
    "jobs",
    "sermons",
    "sermon_revisions",
    "approvals",
    "publications",
    "audit_log",
}


def test_upgrade_from_empty_creates_schema(db_path: Path, engine: Engine) -> None:
    assert current_revision(db_path) == head_revision()
    assert set(inspect(engine).get_table_names()) >= TABLES


def test_connections_use_wal_and_foreign_keys(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_upgrade_is_idempotent_and_reversible(db_path: Path) -> None:
    upgrade(db_path)
    command.downgrade(alembic_config(db_path), "base")
    assert current_revision(db_path) is None
    upgrade(db_path)
    assert current_revision(db_path) == head_revision()


def test_cli_db_upgrade_with_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "sub" / "g.sqlite3"
    assert main(["db", "upgrade", "--database", str(path)]) == 0
    assert current_revision(path) == head_revision()
    assert str(head_revision()) in capsys.readouterr().out


def test_cli_db_upgrade_uses_config_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config = tmp_path / "georg.toml"
    config.write_text(
        f"""
instance = "test"
data_dir = "{data_dir}"

[[inputs]]
name = "local"
type = "local_dir"

[processors]
classifier = {{ type = "fake" }}
detector = {{ type = "fake" }}
transcriber = {{ type = "fake" }}
formatter = {{ type = "fake" }}
metadata = {{ type = "fake" }}
""",
        encoding="utf-8",
    )
    assert main(["db", "upgrade", "--config", str(config)]) == 0
    engine = create_engine(data_dir / "georg.sqlite3")
    try:
        assert set(inspect(engine).get_table_names()) >= TABLES
    finally:
        engine.dispose()


def test_cli_reports_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "upgrade", "--config", str(tmp_path / "missing.toml")]) == 1
    assert "missing.toml" in capsys.readouterr().err


def test_upgrade_marks_existing_failed_jobs_as_notified(db_path: Path, tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    upgrade(path, "0001")
    engine = create_engine(path)
    try:
        with engine.begin() as conn:
            for status in ("failed", "queued"):
                conn.execute(
                    text(
                        "INSERT INTO jobs (kind, payload_json, status, run_after, attempts,"
                        " max_attempts, created_at, finished_at) VALUES ('discover', '{}',"
                        " :status, '2026-11-01', 1, 5, '2026-11-01', '2026-11-02')"
                    ),
                    {"status": status},
                )
        upgrade(path)
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT status, failure_notified_at FROM jobs ORDER BY id")
            ).all()
    finally:
        engine.dispose()
    assert [(r[0], r[1] is not None) for r in rows] == [("failed", True), ("queued", False)]


def test_upgrade_recreates_tables_that_other_rows_reference(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    upgrade(path, "0002")
    engine = create_engine(path)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, role, created_at)"
                    " VALUES (1, 'xan', 'x', 'admin', '2026-11-01')"
                )
            )
            for sql in (
                "INSERT INTO services (id, source_type, source_ref, hints_json, discovered_at)"
                " VALUES (1, 'local_folder', 'a', '{}', '2026-11-01')",
                "INSERT INTO sermons (id, service_id) VALUES (1, 1)",
                "INSERT INTO sermon_revisions (id, sermon_id, number, author, service_start_ms,"
                " sermon_start_ms, sermon_end_ms, service_end_ms, metadata_json,"
                " field_origins_json, content_hash, created_at)"
                " VALUES (1, 1, 1, 'system', 0, 1, 2, 3, '{}', '{}', 'h', '2026-11-01')",
                "UPDATE sermons SET current_revision_id = 1",
                "INSERT INTO approvals (revision_id, approved_by, approved_at)"
                " VALUES (1, 1, '2026-11-02')",
            ):
                conn.execute(text(sql))
        upgrade(path)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT username, role FROM users")).all() == [
                ("xan", "admin")
            ]
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_pending_migration_writes_a_consistent_backup(tmp_path: Path) -> None:
    path = tmp_path / "g.sqlite3"
    upgrade(path, "0001")
    engine = create_engine(path)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, role, created_at)"
                    " VALUES (1, 'xan', 'x', 'admin', '2026-11-01')"
                )
            )
    finally:
        engine.dispose()
    upgrade(path)
    (backup,) = (tmp_path / "backups").glob("georg-0001-*.sqlite")
    assert current_revision(backup) == "0001"
    copy = create_engine(backup)
    try:
        with copy.connect() as conn:
            assert conn.execute(text("SELECT username FROM users")).all() == [("xan",)]
    finally:
        copy.dispose()


def test_upgrade_at_head_takes_no_backup(db_path: Path) -> None:
    upgrade(db_path)
    assert not (db_path.parent / "backups").exists()


def test_upgrade_refuses_a_database_from_a_newer_georg(tmp_path: Path) -> None:
    path = tmp_path / "g.sqlite3"
    upgrade(path)
    engine = create_engine(path)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = 'newer00'"))
    finally:
        engine.dispose()
    with pytest.raises(GeorgError, match="newer00"):
        upgrade(path)


def test_prune_backups_keeps_the_newest(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    names = [f"georg-0001-2026010{i}T000000Z.sqlite" for i in range(7)]
    for name in names:
        (backups / name).touch()
    removed = prune_backups(tmp_path / "georg.sqlite3", keep=5)
    assert {p.name for p in removed} == set(names[:2])
    assert sorted(p.name for p in backups.iterdir()) == names[2:]


def test_backup_database_names_unstamped_databases(tmp_path: Path) -> None:
    path = tmp_path / "g.sqlite3"
    engine = create_engine(path)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
    finally:
        engine.dispose()
    backup = backup_database(path, None)
    assert backup.name.startswith("georg-unstamped-")


def test_db_wait_idle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "g.sqlite3"
    idle = ["db", "wait-idle", "--database", str(path), "--timeout", "0.05", "--interval", "0.01"]
    assert main(idle) == 0  # no database yet
    upgrade(path)
    assert main(idle) == 0  # migrated, no jobs
    from georg.jobs.queue import JobQueue

    engine = create_engine(path)
    try:
        queue = JobQueue(engine)
        queue.enqueue("discover")
        assert queue.lease("test") is not None
    finally:
        engine.dispose()
    assert main(idle) == 1  # a running job outlives the timeout
    assert "still running" in capsys.readouterr().err
