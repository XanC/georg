# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite engine setup.

Every connection uses WAL journaling, enforces foreign keys, and waits for locks
instead of failing at once. pysqlite's implicit transaction handling is disabled so
that SQLAlchemy emits `BEGIN` itself; engines returned by `writer` emit
`BEGIN IMMEDIATE`, which takes the write lock up front so that read-then-write
transactions in concurrent processes serialize instead of failing.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, event
from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy.orm import Session, sessionmaker

DB_FILENAME = "georg.sqlite3"
BUSY_TIMEOUT_MS = 30_000
_IMMEDIATE = "georg_begin_immediate"


def database_path(data_dir: Path) -> Path:
    return data_dir / DB_FILENAME


def sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def create_engine(path: Path, *, echo: bool = False) -> Engine:
    engine = _sa_create_engine(
        sqlite_url(path), echo=echo, connect_args={"timeout": BUSY_TIMEOUT_MS / 1000}
    )
    event.listen(engine, "connect", _on_connect)
    event.listen(engine, "begin", _on_begin)
    return engine


def writer(engine: Engine) -> Engine:
    """The same engine, but every transaction starts with `BEGIN IMMEDIATE`."""
    return engine.execution_options(**{_IMMEDIATE: True})


def session_factory(engine: Engine, *, immediate: bool = True) -> sessionmaker[Session]:
    return sessionmaker(bind=writer(engine) if immediate else engine, expire_on_commit=False)


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def _on_begin(conn: Connection) -> None:
    immediate = bool(conn.get_execution_options().get(_IMMEDIATE, False))
    conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")
