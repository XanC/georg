# SPDX-License-Identifier: AGPL-3.0-only
"""Georg command-line interface."""

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from georg import __version__
from georg.core.errors import GeorgError

DEFAULT_CONFIG = Path("georg.toml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="georg", description=__doc__)
    parser.add_argument("--version", action="version", version=f"georg {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    db = commands.add_parser("db", help="manage the database")
    db_commands = db.add_subparsers(dest="db_command", metavar="COMMAND", required=True)
    upgrade = db_commands.add_parser("upgrade", help="apply database migrations")
    upgrade.add_argument(
        "--config",
        type=Path,
        help=f"configuration file (default: $GEORG_CONFIG or ./{DEFAULT_CONFIG})",
    )
    upgrade.add_argument(
        "--database", type=Path, help="SQLite file (default: georg.sqlite3 in data_dir)"
    )
    upgrade.add_argument("--revision", default="head", help="target revision (default: head)")
    upgrade.set_defaults(handler=_db_upgrade)
    return parser


def _database_path(args: argparse.Namespace) -> Path:
    if args.database is not None:
        return Path(args.database)
    from georg.core.config import load_config
    from georg.db.engine import database_path

    config_path = args.config or Path(os.environ.get("GEORG_CONFIG", DEFAULT_CONFIG))
    return database_path(load_config(config_path).data_dir)


def _db_upgrade(args: argparse.Namespace) -> int:
    from georg.db.migrate import current_revision, upgrade

    path = _database_path(args)
    upgrade(path, args.revision)
    print(f"{path}: at revision {current_revision(path)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        return 0
    try:
        return handler(args)
    except GeorgError as exc:
        print(f"georg: {exc}", file=sys.stderr)
        return 1
