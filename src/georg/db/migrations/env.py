# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path

from alembic import context

from georg.db.engine import create_engine
from georg.db.models import Base

config = context.config
db_path = config.attributes.get("db_path")
if not isinstance(db_path, Path):
    raise RuntimeError("run migrations through georg.db.migrate (e.g. `georg db upgrade`)")

engine = create_engine(db_path)
try:
    with engine.connect() as connection:
        # Batch migrations recreate tables, and dropping a table that other rows
        # reference fails while foreign keys are enforced. SQLite ignores this pragma
        # inside a transaction, so it is set on the raw connection before one starts.
        raw = connection.connection.driver_connection
        assert raw is not None
        raw.execute("PRAGMA foreign_keys=OFF")
        try:
            context.configure(
                connection=connection, target_metadata=Base.metadata, render_as_batch=True
            )
            with context.begin_transaction():
                context.run_migrations()
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                if violations:
                    raise RuntimeError(f"migration broke foreign keys: {violations}")
        finally:
            raw.execute("PRAGMA foreign_keys=ON")
finally:
    engine.dispose()
