# SPDX-License-Identifier: AGPL-3.0-only
"""Run Alembic migrations without an alembic.ini file."""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from georg.core.errors import GeorgError
from georg.db.engine import create_engine, sqlite_url

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BACKUPS_DIRNAME = "backups"
KEEP_BACKUPS = 5


def alembic_config(db_path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", sqlite_url(db_path))
    config.attributes["db_path"] = db_path
    return config


def scripts() -> ScriptDirectory:
    return ScriptDirectory(str(MIGRATIONS_DIR))


def backup_database(db_path: Path, from_revision: str | None) -> Path:
    """Write a consistent copy of the database via the SQLite backup API.

    Safe to run while Litestream replicates the same file: the backup API takes a
    read snapshot through SQLite itself and never touches the -wal/-shm files.
    """
    backups_dir = db_path.parent / BACKUPS_DIRNAME
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest_path = backups_dir / f"georg-{from_revision or 'unstamped'}-{stamp}.sqlite"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return dest_path


def prune_backups(db_path: Path, keep: int = KEEP_BACKUPS) -> list[Path]:
    """Delete all but the `keep` newest backup files; return the deleted paths."""
    backups_dir = db_path.parent / BACKUPS_DIRNAME
    backups = sorted(backups_dir.glob("georg-*.sqlite"), key=lambda p: p.name)
    removed = []
    for path in backups[:-keep] if keep else backups:
        path.unlink()
        removed.append(path)
    return removed


def upgrade(db_path: Path, revision: str = "head") -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    script = scripts()
    try:
        target = script.get_revision(revision).revision
    except CommandError:
        raise GeorgError(f"unknown database revision {revision!r}") from None
    from_revision = current_revision(db_path)
    if from_revision is not None:
        try:
            script.get_revision(from_revision)
        except CommandError:
            raise GeorgError(
                f"{db_path} is at database revision {from_revision!r}, which this "
                "version of Georg does not know: the database was migrated by a newer "
                "Georg. Run that newer version instead of downgrading."
            ) from None
    # A fresh database has no stamped revision yet: nothing to back up.
    if from_revision is not None and from_revision != target:
        backup = backup_database(db_path, from_revision)
        log.info("database backed up to %s before migrating", backup)
        for removed in prune_backups(db_path):
            log.info("old database backup %s removed", removed)
    command.upgrade(alembic_config(db_path), revision)


def current_revision(db_path: Path) -> str | None:
    engine = create_engine(db_path)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def head_revision() -> str | None:
    return scripts().get_current_head()
