# SPDX-License-Identifier: AGPL-3.0-only
"""Georg command-line interface."""

import argparse
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from georg import __version__
from georg.core.config import RenditionSpec
from georg.core.errors import GeorgError
from georg.core.types import ServiceBoundaries, TimeRange
from georg.db.models import USER_ROLES

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from georg.core.config import GeorgConfig
    from georg.core.contracts import OutputTarget

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


def _db_wait_idle(args: argparse.Namespace) -> int:
    """Exit 0 once the jobs table has no running job, else 1 after --timeout."""
    import time

    from sqlalchemy import inspect

    from georg.db.engine import create_engine
    from georg.jobs.queue import JobQueue

    path = _database_path(args)
    engine = create_engine(path)
    try:
        if not inspect(engine).has_table("jobs"):
            print(f"{path}: no jobs table; nothing running")
            return 0
        queue = JobQueue(engine)
        deadline = time.monotonic() + args.timeout
        while True:
            running = queue.counts().get("running", 0)
            if running == 0:
                print(f"{path}: no running jobs")
                return 0
            if time.monotonic() >= deadline:
                print(
                    f"georg: {running} job(s) still running after {args.timeout:.0f}s",
                    file=sys.stderr,
                )
                return 1
            print(f"{running} job(s) running; waiting up to {args.timeout:.0f}s")
            time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))
    finally:
        engine.dispose()


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


def _document_texts(documents: list[str]) -> dict[str, str]:
    """Text of the `--document` arguments that are readable local files."""
    from georg.documents import extract_text

    texts = {}
    for document in documents:
        path = Path(document)
        if path.is_file() and (text := extract_text(path)):
            texts[path.name] = text
    return texts


def _run(args: argparse.Namespace) -> int:
    from georg.core.config import load_config
    from georg.pipeline import StageCache, build_processors, run_local

    config = load_config(args.config)
    processors = build_processors(
        config.processors, formatter=not args.no_format, calendar=config.calendar
    )
    result = run_local(
        args.parts,
        args.out_dir,
        processors,
        cache=StageCache(args.cache or config.data_dir / "cache"),
        renditions=config.renditions,
        language=config.language,
        service_date=args.date,
        documents=[Path(d).name for d in args.document],
        document_texts=_document_texts(args.document),
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


def _cleanup(args: argparse.Namespace) -> int:
    from georg.core.config import RetentionConfig
    from georg.db.engine import session_factory
    from georg.jobs.queue import JobQueue
    from georg.pipeline.retention import cleanup

    config, engine = _open(args)
    if args.days is not None:
        config = config.model_copy(update={"retention": RetentionConfig(days=args.days)})
    if config.retention.days <= 0:
        print("retention is off ([retention] days = 0); pass --days N to preview a cleanup")
        return 0
    report = cleanup(config, session_factory(engine), JobQueue(engine), dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    for purge in report.purges:
        print(
            f"service {purge.service_id} ({purge.title}): {purge.reason} "
            f"{purge.since:%Y-%m-%d}; {verb} {len(purge.files)} files, {purge.bytes} bytes"
        )
        if args.dry_run:
            for path in purge.files:
                print(f"  {path}")
    for service_id in report.skipped_busy:
        print(f"service {service_id}: skipped, it has queued or running jobs")
    if report.stale_cache:
        print(
            f"unowned cache files older than {report.days} days: {verb} "
            f"{len(report.stale_cache)} files, {report.stale_cache_bytes} bytes"
        )
    print(
        f"{len(report.purges)} service(s), {report.bytes} bytes "
        + ("would be freed (dry run)" if args.dry_run else "freed")
    )
    return 0


WP_CLI = "sudo -u www-data php /usr/local/lib/wp-cli/wp-cli.phar --path=/var/www/html"
"""How WP-CLI is invoked on a hosted church's web server."""


@dataclass(frozen=True)
class _KeyChange:
    """One publications row whose key moves from the instance slug to the church UUID."""

    publication_id: int
    target_name: str
    old_key: str
    new_key: str
    external_id: str | None


def _wp_rekey_script(changes: Sequence[_KeyChange]) -> str:
    """A WP-CLI shell script re-keying `_georg_publication_key` on posts and attachments.

    Each `post list` selects only posts whose meta still holds the old key, so
    re-running the script changes nothing. The two post types georg-bridge
    writes the key on get their own query — `ctc_sermon` posts at any status
    and audio `attachment`s (status `inherit`, which "any" excludes) — and
    `post list` runs in an assignment, not the `for` line, so `set -e` stops
    the script when a lookup fails instead of re-keying nothing.
    """
    lines = [
        "#!/bin/sh",
        "# Re-key Georg's WordPress publications to the church-UUID key form.",
        "# Run on the church web server; idempotent.",
        "set -eu",
        f'WP="{WP_CLI}"',
        "",
    ]
    for change in changes:
        lines += [
            f"# publication {change.publication_id} ({change.target_name})",
            'IDS="$($WP post list --post_type=ctc_sermon --post_status=any'
            f" --meta_key=_georg_publication_key --meta_value='{change.old_key}'"
            ' --field=ID)"',
            'IDS="$IDS $($WP post list --post_type=attachment'
            " --post_status=inherit,private"
            f" --meta_key=_georg_publication_key --meta_value='{change.old_key}'"
            ' --field=ID)"',
            "for ID in $IDS; do",
            f"  $WP post meta update \"$ID\" _georg_publication_key '{change.new_key}'",
            "done",
            "",
        ]
    return "\n".join(lines)


def _migrate_publication_keys(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from georg.db.engine import session_factory
    from georg.db.models import Publication
    from georg.db.repositories import IdentityRepository
    from georg.pipeline.registry import build_output

    config, engine = _open(args)
    sessions = session_factory(engine)
    old_prefix = f"georg:{args.old_instance}:"
    outputs: dict[str, OutputTarget] = {}

    def rows() -> list[tuple[int, str, str, str | None]]:
        with sessions() as session:
            pubs = session.scalars(select(Publication).order_by(Publication.id)).all()
            return [(p.id, p.target_name, p.publication_key, p.external_id) for p in pubs]

    def output(name: str) -> "OutputTarget":
        if name not in outputs:
            outputs[name] = build_output(targets[name], timezone=config.timezone)
        return outputs[name]

    with sessions() as session:
        church_id = IdentityRepository(session).church_id()
    new_prefix = f"georg:{church_id}:"
    targets = {c.name: c for c in config.outputs}

    plan: list[_KeyChange] = []
    already = 0
    for pid, target, key, ext in rows():
        if key.startswith(old_prefix):
            plan.append(_KeyChange(pid, target, key, new_prefix + key[len(old_prefix) :], ext))
        elif key.startswith(new_prefix):
            already += 1
        else:
            print(f"publication {pid}: key {key!r} matches neither key form; left alone")
    print(f"church_id: {church_id}")
    print(f"{already} publication(s) already on the new key; {len(plan)} to migrate")
    for change in plan:
        print(f"  {change.old_key} -> {change.new_key}")
        if change.target_name not in targets:
            print(
                f"  warning: target {change.target_name!r} is not configured; "
                "only its database row can change"
            )

    wp_changes = [
        c for c in plan if targets.get(c.target_name) and targets[c.target_name].type == "wordpress"
    ]
    drive_changes = [
        c
        for c in plan
        if targets.get(c.target_name) and targets[c.target_name].type == "google_drive_upload"
    ]
    if wp_changes:
        script_path = args.wp_script or Path(f"wp-rekey-{args.old_instance}.sh")
        script_path.write_text(_wp_rekey_script(wp_changes))
        script_path.chmod(0o755)
        print(
            f"wrote {script_path}: run it on the church web server, then re-run "
            "this command with --apply"
        )

    if args.apply:
        for change in drive_changes:
            moved = output(change.target_name).rekey(  # type: ignore[attr-defined]
                change.old_key, change.new_key, file_id=change.external_id
            )
            if moved is None:
                print(
                    f"publication {change.publication_id}: no Drive file carries {change.old_key!r}"
                )
            else:
                print(f"publication {change.publication_id}: Drive file {moved} re-keyed")
        with sessions.begin() as session:
            updated = 0
            for change in plan:
                pub = session.get(Publication, change.publication_id)
                if pub is not None and pub.publication_key == change.old_key:
                    pub.publication_key = change.new_key
                    updated += 1
        print(f"updated {updated} publication row(s)")

    if args.verify:
        missing = unverifiable = 0
        for pid, target, key, _ in rows():
            if key.startswith(old_prefix):
                print(f"publication {pid} ({target}): still on the old key; run --apply")
                missing += 1
                continue
            cfg = targets.get(target)
            if cfg is None or cfg.type not in ("wordpress", "google_drive_upload"):
                print(f"publication {pid} ({target}): not externally verifiable")
                unverifiable += 1
                continue
            if output(target).find_existing(key) is None:
                print(f"publication {pid} ({target}): NOT FOUND: {key}")
                missing += 1
            else:
                print(f"publication {pid} ({target}): found at target")
        print(f"verify: {missing} missing, {unverifiable} unverifiable")
        return 1 if missing else 0

    if not args.apply and not args.verify:
        print("dry run: run the WordPress script, then re-run with --apply, then --verify")
    return 0


def _export(args: argparse.Namespace) -> int:
    from georg.db.engine import database_path
    from georg.transfer import export_bundle

    config, engine = _open(args)
    try:
        report = export_bundle(config, engine, database_path(config.data_dir), args.out)
    finally:
        engine.dispose()
    print(f"wrote {report.bundle} (church {report.church_id})")
    print(f"{report.artifacts} artifact file(s); {report.cells_rewritten} path(s) made relative")
    if report.missing_artifacts:
        print(f"warning: {len(report.missing_artifacts)} artifact(s) were missing from the store")
    print("this instance is now frozen: discovery and publishing here stop for good")
    return 0


def _import(args: argparse.Namespace) -> int:
    from georg.core.config import load_config
    from georg.transfer import import_bundle

    config = load_config(args.config)
    report = import_bundle(config, args.bundle)
    print(f"imported church {report.church_id}; {report.artifacts_restored} artifact(s) restored")
    if report.missing_artifacts:
        print(
            f"warning: {len(report.missing_artifacts)} artifact(s) were not in the source "
            "store and are not in the bundle"
        )
    if report.external_paths:
        print("paths recorded in the bundle that may not exist on this host:")
        for path in report.external_paths:
            print(f"  {path}")
    print(
        "the instance is paused: write this host's georg.toml and secrets, then run `georg resume`"
    )
    return 0


def _resume(args: argparse.Namespace) -> int:
    from georg.db.engine import session_factory
    from georg.jobs.queue import JobQueue
    from georg.transfer import resume

    config, engine = _open(args)
    try:
        report = resume(config, session_factory(engine), queue=JobQueue(engine), force=args.force)
    finally:
        engine.dispose()
    if report.state == "active" and not report.resumed and not report.checked:
        print("this instance is already active")
        return 0
    for line in report.missing:
        print(f"missing at target: {line}")
    for line in report.unreachable:
        print(f"target unreachable: {line}")
    for line in report.unverifiable:
        print(f"cannot verify: {line}")
    if not report.resumed:
        print(
            f"georg: {report.problems} problem(s) checking {report.checked} "
            "publication(s); not resuming (use --force to resume anyway)",
            file=sys.stderr,
        )
        return 1
    qualifier = " (forced)" if report.problems else ""
    print(f"checked {report.checked} publication(s); instance resumed{qualifier}")
    return 0


WATCHDOG_POLL_SECONDS = 10.0
"""Upper bound on the idle poll while the watchdog is on, so the browser's one-minute
shutdown warning is accurate to a few seconds."""


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
        poll = args.poll if watchdog is None else min(args.poll, WATCHDOG_POLL_SECONDS)
        worker.run(poll_seconds=poll)
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
        "--document",
        action="append",
        default=[],
        help="companion document name or local file to read (repeatable)",
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

    clean = commands.add_parser(
        "cleanup",
        help="delete stored files of services past [retention] days",
        description="Delete the audio, renditions, previews, transcripts and cached stage "
        "outputs of services whose sermon was published through Georg [retention] days ago "
        "(or discovered that long ago, if never published). Database rows stay, so "
        "discovery never fetches them again and publishing updates the existing posts.",
    )
    clean.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    clean.add_argument(
        "--dry-run", action="store_true", help="list what would be deleted, delete nothing"
    )
    clean.add_argument(
        "--days",
        type=int,
        help="use this retention instead of [retention] days (works while cleanup is off)",
    )
    clean.set_defaults(handler=_cleanup)

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
    user_add.add_argument("--role", choices=USER_ROLES, required=True)
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

    migrate_keys = commands.add_parser(
        "migrate-publication-keys",
        help="re-key publications from the instance slug to the church UUID",
        description="One-time migration after the church-UUID change (ADR 0003): "
        "lists publications still keyed georg:<old-instance>:..., writes a WP-CLI "
        "script for the WordPress targets, and with --apply re-keys Google Drive "
        "files and the database. --verify checks each new key at its target. "
        "Idempotent; safe to re-run.",
    )
    migrate_keys.add_argument(
        "--old-instance",
        required=True,
        help="the [instance] slug the old keys were built from, e.g. stpaul",
    )
    migrate_keys.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    migrate_keys.add_argument(
        "--wp-script",
        type=Path,
        help="where to write the WP-CLI script (default: ./wp-rekey-<old-instance>.sh)",
    )
    migrate_keys.add_argument(
        "--apply",
        action="store_true",
        help="re-key Drive files and the database rows (dry run without it)",
    )
    migrate_keys.add_argument(
        "--verify",
        action="store_true",
        help="call each target's find_existing on every publication's new key",
    )
    migrate_keys.set_defaults(handler=_migrate_publication_keys)

    export = commands.add_parser(
        "export",
        help="write a portable bundle and freeze this instance",
        description="Write one .georg tar (manifest, consistent database copy, artifact "
        "files) that `georg import` restores on another host, whatever its filesystem "
        "layout. Exporting freezes this instance: discovery and publishing here stop "
        "for good, so two deployments never publish the same church. The configuration "
        "and secrets are not in the bundle; the new host's operator writes georg.toml.",
    )
    export.add_argument("--out", type=Path, required=True, help="bundle file to write")
    export.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    export.set_defaults(handler=_export)

    imp = commands.add_parser(
        "import",
        help="restore a .georg bundle into an empty data dir",
        description="Restore a bundle made by `georg export` into this config's "
        "data_dir (which must not hold a database yet): the database, keeping the "
        "church UUID and publication history, and the artifact files. A bundle from "
        "a newer Georg is refused. The instance starts paused; `georg resume` "
        "verifies the published objects at their targets and unpauses.",
    )
    imp.add_argument("bundle", type=Path, help=".georg bundle file")
    imp.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    imp.set_defaults(handler=_import)

    resume = commands.add_parser(
        "resume",
        help="verify publications and unpause an imported instance",
        description="Check every published publication against its target's "
        "find_existing(publication_key), then unpause discovery and publishing. "
        "Missing or unreachable targets refuse the resume unless --force.",
    )
    resume.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    resume.add_argument(
        "--force",
        action="store_true",
        help="resume even when publications are missing or targets unreachable",
    )
    resume.set_defaults(handler=_resume)

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

    wait_idle = db_commands.add_parser(
        "wait-idle",
        help="exit 0 once no job is running (for deploy/upgrade.sh)",
        description="Poll the jobs table until no job has status 'running', then exit 0. "
        "Exits 1 when --timeout seconds pass first. Does not run migrations, so the "
        "old image can run it while an upgrade is pending.",
    )
    wait_idle.add_argument(
        "--config",
        type=Path,
        help=f"configuration file (default: $GEORG_CONFIG or ./{DEFAULT_CONFIG})",
    )
    wait_idle.add_argument(
        "--database", type=Path, help="SQLite file (default: georg.sqlite3 in data_dir)"
    )
    wait_idle.add_argument(
        "--timeout", type=float, default=600.0, help="seconds to wait (default: 600)"
    )
    wait_idle.add_argument(
        "--interval", type=float, default=5.0, help="seconds between checks (default: 5)"
    )
    wait_idle.set_defaults(handler=_db_wait_idle)
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
