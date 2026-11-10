# SPDX-License-Identifier: AGPL-3.0-only
"""Georg command-line interface."""

import argparse
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from georg import __version__
from georg.core.config import RenditionSpec
from georg.core.errors import GeorgError
from georg.core.types import ServiceBoundaries, TimeRange

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from georg.core.config import GeorgConfig

DEFAULT_CONFIG = Path("georg.toml")
SAMPLE_MS = 120_000
SAMPLE_SPEC = RenditionSpec(name="sample", span="service", channels=2, vbr_quality=2)
_TIMESTAMP = re.compile(r"^(?:(?:(\d+):)?(\d{1,2}):)?(\d+(?:\.\d+)?)$")


def parse_timestamp(value: str) -> int:
    """Parse "SS[.mmm]", "MM:SS[.mmm]" or "HH:MM:SS[.mmm]" into milliseconds."""
    match = _TIMESTAMP.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"invalid timestamp {value!r}")
    hours, minutes, seconds = match.groups()
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)
    return round(total * 1000)


def master_sample(src: Path, out_dir: Path, span: TimeRange) -> tuple[Path, Path]:
    """Write an A/B pair for `span` of `src`: gain-only (A) and the mastering chain (B)."""
    from georg.media import cut, encode_mp3, extract_audio
    from georg.media.mastering import master, normalize_gain

    out_dir.mkdir(parents=True, exist_ok=True)
    a_path, b_path = out_dir / "A_unprocessed.mp3", out_dir / "B_mastered.mp3"
    with tempfile.TemporaryDirectory(dir=out_dir) as work:
        tmp = Path(work)
        audio = extract_audio([src], tmp / "source.flac")
        excerpt = cut(audio, span, tmp / "excerpt.flac")
        encode_mp3(normalize_gain(excerpt, tmp / "a.flac"), a_path, SAMPLE_SPEC)
        encode_mp3(master(excerpt, tmp / "b.flac"), b_path, SAMPLE_SPEC)
    return a_path, b_path


def _master_sample(args: argparse.Namespace) -> int:
    end = args.end if args.end is not None else args.start + SAMPLE_MS
    if end <= args.start:
        raise GeorgError("--end must be after --start")
    for path in master_sample(args.input, args.out_dir, TimeRange(start_ms=args.start, end_ms=end)):
        print(path)
    return 0


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


def parse_boundaries(value: str) -> ServiceBoundaries:
    """Parse "service_start,sermon_start,sermon_end,service_end" timestamps."""
    times = [parse_timestamp(v) for v in value.split(",")]
    if len(times) != 4:
        raise argparse.ArgumentTypeError("expected four comma-separated timestamps")
    try:
        return ServiceBoundaries(
            service_start_ms=times[0],
            sermon_start_ms=times[1],
            sermon_end_ms=times[2],
            service_end_ms=times[3],
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _fmt_ms(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _run(args: argparse.Namespace) -> int:
    from georg.core.config import load_config
    from georg.pipeline import StageCache, build_processors, run_local

    config = load_config(args.config)
    processors = build_processors(config.processors, formatter=not args.no_format)
    result = run_local(
        args.parts,
        args.out_dir,
        processors,
        cache=StageCache(args.cache or config.data_dir / "cache"),
        renditions=config.renditions,
        language=config.language,
        service_date=args.date,
        documents=args.document,
        boundaries=args.boundaries,
    )
    b = result.boundaries
    print(
        "boundaries: "
        + ", ".join(
            _fmt_ms(ms)
            for ms in (b.service_start_ms, b.sermon_start_ms, b.sermon_end_ms, b.service_end_ms)
        )
        + f" (confidence {result.proposal.confidence:.2f})"
    )
    for name, path in result.files.items():
        print(f"{name}: {path}")
    return 0


def _open(args: argparse.Namespace) -> tuple["GeorgConfig", "Engine"]:
    from georg.core.config import load_config
    from georg.db import create_engine, upgrade
    from georg.db.engine import database_path

    config = load_config(args.config)
    path = database_path(config.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    upgrade(path)
    return config, create_engine(path)


def _discover(args: argparse.Namespace) -> int:
    from georg.core.config import DiscoveryConfig
    from georg.db.engine import session_factory
    from georg.jobs.queue import JobQueue
    from georg.pipeline import discover

    config, engine = _open(args)
    if args.since is not None:
        config = config.model_copy(update={"discovery": DiscoveryConfig(start_date=args.since)})
    new = discover(config, session_factory(engine), JobQueue(engine))
    print(f"{len(new)} new service(s) queued" + (f": {new}" if new else ""))
    return 0


def _reprocess(args: argparse.Namespace) -> int:
    from georg.db.engine import session_factory
    from georg.db.repositories import ServiceRepository
    from georg.jobs.queue import JobQueue
    from georg.pipeline.service import PROCESS

    _, engine = _open(args)
    with session_factory(engine)() as session:
        ServiceRepository(session).get(args.service_id)
    job_id = JobQueue(engine).enqueue(PROCESS, {"service_id": args.service_id, "reprocess": True})
    print(f"queued reprocess job {job_id} for service {args.service_id}")
    return 0


def _worker(args: argparse.Namespace) -> int:
    from datetime import timedelta

    from georg.idle import ActivityMarker, IdleWatchdog, run_command
    from georg.pipeline import Worker

    config, engine = _open(args)
    watchdog = None
    if config.idle_shutdown_minutes is not None and not args.once:
        watchdog = IdleWatchdog(
            engine,
            idle_after=timedelta(minutes=config.idle_shutdown_minutes),
            marker=ActivityMarker.for_data_dir(config.data_dir),
            shutdown=run_command(config.idle_shutdown_command),
        )
    worker = Worker(config, engine, watchdog=watchdog)
    if args.once:
        while worker.run_once():
            pass
    else:
        worker.run(poll_seconds=args.poll)
    return 0


def _web(args: argparse.Namespace) -> int:
    import secrets

    import uvicorn

    from georg.web import create_app
    from georg.web.app import SECRET_ENV

    secret = os.environ.get(SECRET_ENV)
    if not secret:
        if not args.dev:
            raise GeorgError(f"{SECRET_ENV} is not set (use --dev for a throwaway key)")
        secret = secrets.token_urlsafe(32)
        logging.getLogger(__name__).warning("--dev: throwaway session key; logins end on restart")
    config, engine = _open(args)
    app = create_app(config, engine, secret_key=secret, https_only=not args.dev)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)
    return 0


def _read_password(from_stdin: bool) -> str:
    import getpass

    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            raise GeorgError("passwords do not match")
    if len(password) < 8:
        raise GeorgError("password must be at least 8 characters")
    return password


def _user_add(args: argparse.Namespace) -> int:
    from sqlalchemy.exc import IntegrityError

    from georg.db.engine import session_factory
    from georg.db.repositories import UserRepository
    from georg.web.auth import hash_password

    _, engine = _open(args)
    password_hash = hash_password(_read_password(args.password_stdin))
    try:
        with session_factory(engine).begin() as session:
            user = UserRepository(session).add(args.username, password_hash, args.role)
            user_id = user.id
    except IntegrityError:
        raise GeorgError(f"user {args.username!r} already exists") from None
    print(f"added {args.role} {args.username!r} (id {user_id})")
    return 0


def _drive_login(args: argparse.Namespace) -> int:
    from georg.gdrive import SCOPE_DRIVE, SCOPE_READONLY
    from georg.gdrive.login import load_client, login

    email = login(
        load_client(args.client),
        args.out,
        scope=SCOPE_READONLY if args.read_only else SCOPE_DRIVE,
        paste=args.no_browser,
    )
    print(f"signed in as {email or 'an unknown account'}; credentials written to {args.out}")
    print("point the Drive modules' credentials_file (or $GOOGLE_APPLICATION_CREDENTIALS) at it")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="georg", description=__doc__)
    parser.add_argument("--version", action="version", version=f"georg {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    sample = commands.add_parser(
        "master-sample",
        help="write an A/B pair (gain-only vs mastered) for listening comparisons",
        description="A is the excerpt with a single gain toward the loudness target and no "
        "compression (like the legacy wavegain step alone); B is the full mastering chain. "
        "Both are encoded as stereo MP3 VBR V2.",
    )
    sample.add_argument("input", type=Path, help="recording (audio or video)")
    sample.add_argument("out_dir", type=Path)
    sample.add_argument("--start", type=parse_timestamp, required=True, help="e.g. 0:31:20")
    sample.add_argument("--end", type=parse_timestamp, help="default: start + 2 minutes")
    sample.set_defaults(handler=_master_sample)

    run = commands.add_parser(
        "run",
        help="process local recording files without the database",
        description="Process a recording (its parts in order) into OUT_DIR: renditions, "
        "sermon transcript, and report.json with the detected boundaries.",
    )
    run.add_argument("parts", type=Path, nargs="+", help="recording parts, in order")
    run.add_argument("--out", dest="out_dir", type=Path, required=True)
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    run.add_argument("--cache", type=Path, help="stage cache (default: <data_dir>/cache)")
    run.add_argument("--date", type=date.fromisoformat, help="service date, YYYY-MM-DD")
    run.add_argument(
        "--document", action="append", default=[], help="companion document name (repeatable)"
    )
    run.add_argument(
        "--boundaries",
        type=parse_boundaries,
        help="override detection: service_start,sermon_start,sermon_end,service_end",
    )
    run.add_argument("--no-format", action="store_true", help="skip the transcript formatter")
    run.set_defaults(handler=_run)

    disc = commands.add_parser("discover", help="find new recordings and queue them")
    disc.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    disc.add_argument(
        "--since",
        type=date.fromisoformat,
        help="only services on or after YYYY-MM-DD; overrides [discovery] start_date",
    )
    disc.set_defaults(handler=_discover)

    reprocess = commands.add_parser(
        "reprocess",
        help="queue a service for processing again",
        description="Queue a process job for a service that already has a proposal, "
        "e.g. after a processor upgrade. The new proposal is added as a new revision; "
        "cached stages (extracted audio, classification) are reused. Refused by the "
        "worker once the sermon has a publication record.",
    )
    reprocess.add_argument("service_id", type=int)
    reprocess.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    reprocess.set_defaults(handler=_reprocess)

    work = commands.add_parser("worker", help="run queued jobs")
    work.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    work.add_argument("--once", action="store_true", help="exit when the queue is empty")
    work.add_argument("--poll", type=float, default=30.0, help="seconds between idle polls")
    work.set_defaults(handler=_worker)

    web = commands.add_parser(
        "web",
        help="serve the review UI",
        description="Serve the review UI. Session cookies are signed with $GEORG_SECRET_KEY.",
    )
    web.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument(
        "--dev",
        action="store_true",
        help="allow a throwaway session key and session cookies over plain HTTP",
    )
    web.set_defaults(handler=_web)

    user = commands.add_parser("user", help="manage review UI users")
    user_commands = user.add_subparsers(dest="user_command", metavar="COMMAND", required=True)
    user_add = user_commands.add_parser("add", help="add a user (prompts for the password)")
    user_add.add_argument("username")
    user_add.add_argument("--role", choices=("admin", "reviewer"), required=True)
    user_add.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    user_add.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    user_add.set_defaults(handler=_user_add)

    drive = commands.add_parser(
        "drive-login",
        help="sign in to Google Drive as a Google account",
        description="Sign in once as the Google account Georg reads recordings from and "
        "archives to, and save its refresh token (docs/google-drive.md).",
    )
    drive.add_argument(
        "--client", type=Path, required=True, help="Desktop app OAuth client JSON file"
    )
    drive.add_argument(
        "--out",
        type=Path,
        default=Path("drive-credentials.json"),
        help="credentials file to write (default: ./drive-credentials.json)",
    )
    drive.add_argument(
        "--read-only", action="store_true", help="request read-only access (no archive upload)"
    )
    drive.add_argument(
        "--no-browser",
        action="store_true",
        help="print the address and ask for the redirected one, e.g. over SSH",
    )
    drive.set_defaults(handler=_drive_login)

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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        return 0
    try:
        return handler(args)
    except GeorgError as exc:
        print(f"georg: {exc}", file=sys.stderr)
        return 1
