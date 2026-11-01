# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from georg.cli import main
from georg.db import create_engine, upgrade
from georg.db.migrate import alembic_config, current_revision, head_revision
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
