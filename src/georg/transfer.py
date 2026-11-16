# SPDX-License-Identifier: AGPL-3.0-only
"""`georg export` / `georg import` / `georg resume`: moving an instance.

A bundle is a `.georg` tar holding `manifest.json`, a consistent `db.sqlite3`
(SQLite backup API) and every artifact file still present in the store. Nothing
in it depends on the source's filesystem layout: absolute paths in database text
columns are replaced by tokens, and `import` maps them onto the new `data_dir`.
Secrets and `georg.toml` are never in the bundle — the operator writes the new
host's configuration.

Export freezes the source (`instance_identity.state = 'frozen'`) so two
deployments never publish the same church; an imported instance starts `paused`
until `georg resume` verifies each published object at its target.
"""

import hashlib
import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from alembic.util.exc import CommandError
from sqlalchemy import Engine, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session, sessionmaker

from georg import __version__
from georg.core.config import GeorgConfig
from georg.core.errors import GeorgError
from georg.db.engine import create_engine, database_path, session_factory
from georg.db.migrate import current_revision, scripts, upgrade
from georg.db.models import Job, Publication, Sermon, Service, StageRun
from georg.db.repositories import AuditRepository, IdentityRepository
from georg.jobs.queue import JobQueue
from georg.pipeline.registry import build_output
from georg.pipeline.service import PROCESS, PUBLISH
from georg.storage.local import LocalStore

log = logging.getLogger(__name__)

BUNDLE_FORMAT = 1
MANIFEST_NAME = "manifest.json"
BUNDLE_DB = "db.sqlite3"
ARTIFACTS_DIR = "artifacts"
DATA_DIR_TOKEN = "@georg-data-dir@"
_PATH_TOKEN = "@georg-path-{n}@"
DRAIN_SECONDS = 120.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _backup(db_path: Path, dest: Path) -> None:
    """A consistent copy of `db_path` via the SQLite backup API."""
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _text_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, column) for every declared-text column, alembic's table included."""
    found = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        for row in conn.execute(f"PRAGMA table_info({table})"):
            declared = (row[2] or "").upper()
            if any(t in declared for t in ("CHAR", "TEXT", "CLOB", "JSON")):
                found.append((table, row[1]))
    return found


def _rewrite_paths(db_path: Path, replacements: Mapping[str, str]) -> int:
    """Replace `old` with `new` inside every text column; return cells changed.

    The schema's integrity triggers (immutable revisions and approvals) forbid
    some of these updates, so they are dropped and recreated from their
    definitions — the database keeps them afterwards.
    """
    changed = 0
    conn = sqlite3.connect(db_path)
    try:
        triggers = list(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"))
        for name, _ in triggers:
            conn.execute(f"DROP TRIGGER {name}")
        for table, column in _text_columns(conn):
            for old, new in replacements.items():
                cursor = conn.execute(
                    f"UPDATE {table} SET {column} = replace({column}, ?, ?)"
                    f" WHERE instr({column}, ?) > 0",
                    (old, new, old),
                )
                changed += cursor.rowcount
        for _, sql in triggers:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()
    return changed


def _option_paths(dump: Mapping[str, Any]) -> dict[str, str]:
    """Locator -> absolute path in configured module/section options (input roots,
    model caches, key files): deployment-specific, so they are tokenized on export
    and remapped to the same option's value on the new host on import."""
    found: dict[str, str] = {}

    def walk(locator: str, value: Any) -> None:
        if isinstance(value, Mapping):
            for k, v in value.items():
                walk(f"{locator}.{k}", v)
        elif isinstance(value, list | tuple):
            for i, v in enumerate(value):
                walk(f"{locator}.{i}", v)
        elif isinstance(value, str) and value.startswith("/"):
            found[locator] = value

    for name in ("inputs", "outputs", "processors", "calendar", "review"):
        walk(name, dump[name])
    return found


def _option_at(dump: Mapping[str, Any], locator: str) -> Any:
    """The value at a dotted `section.key[.index]…` locator, or None if absent."""
    node: Any = dump
    for part in locator.split("."):
        if isinstance(node, Mapping):
            node = node.get(part)
        elif isinstance(node, list | tuple) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


@dataclass(frozen=True)
class ExportReport:
    bundle: Path
    church_id: str
    artifacts: int
    missing_artifacts: list[str]
    cells_rewritten: int


def export_bundle(
    config: GeorgConfig,
    engine: Engine,
    db_path: Path,
    out: Path,
    *,
    drain_seconds: float = DRAIN_SECONDS,
) -> ExportReport:
    """Write `out` as a `.georg` bundle and freeze the source instance.

    The source freezes first so no new discovery or publish job starts, then the
    snapshot is taken once already-running jobs have drained — a publication
    committed in between would otherwise be missing from the bundle while the
    source's write went through. If a job is still running after `drain_seconds`
    the export is refused; it can simply be re-run (a frozen instance may export
    again — the freeze is not lifted).
    """
    store = LocalStore.for_data_dir(config.data_dir)
    data_dir = str(config.data_dir.resolve())
    sessions = session_factory(engine)
    queue = JobQueue(engine)
    with sessions.begin() as session:
        identity = IdentityRepository(session)
        church_id = identity.church_id()
        identity.set_state("frozen")
    deadline = time.monotonic() + drain_seconds
    while queue.counts().get("running", 0):
        queue.reclaim_stale()  # a crashed worker's lease otherwise counts forever
        if time.monotonic() >= deadline:
            raise GeorgError(
                "a job is still running; re-run `georg export` when it finishes "
                "(the instance stays frozen)"
            )
        time.sleep(1)
    revision = current_revision(db_path)

    tokens: dict[str, str] = {data_dir: DATA_DIR_TOKEN}
    paths: list[dict[str, Any]] = [
        {"token": DATA_DIR_TOKEN, "path": data_dir, "under_data_dir": True}
    ]
    option_paths = sorted(_option_paths(config.model_dump(mode="json")).items())
    for n, (locator, raw) in enumerate(option_paths):
        resolved = str(Path(raw).resolve())
        if resolved == data_dir or resolved.startswith(data_dir + "/"):
            continue
        token = _PATH_TOKEN.format(n=n)
        tokens[resolved] = token
        tokens[raw] = token
        paths.append({"token": token, "path": resolved, "under_data_dir": False, "option": locator})

    with tempfile.TemporaryDirectory() as work:
        tmp = Path(work)
        snapshot = tmp / BUNDLE_DB
        _backup(db_path, snapshot)
        cells = _rewrite_paths(snapshot, tokens)

        missing: list[str] = []
        conn = sqlite3.connect(snapshot)
        try:
            shas = [row[0] for row in conn.execute("SELECT DISTINCT sha256 FROM artifacts")]
        finally:
            conn.close()
        stored: list[Path] = []
        for sha in shas:
            if store.exists(sha):
                stored.append(store.path(sha))
            else:
                missing.append(sha)

        files = {
            BUNDLE_DB: _sha256(snapshot),
            **{f"{ARTIFACTS_DIR}/{p.name}": _sha256(p) for p in stored},
        }
        manifest = {
            "format": BUNDLE_FORMAT,
            "georg_version": __version__,
            "alembic_revision": revision,
            "church_id": church_id,
            "instance": config.instance,
            "created_at": datetime.now(UTC).isoformat(),
            "paths": paths,
            "files": files,
            "missing_artifacts": missing,
        }
        manifest_path = tmp / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2))
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w") as tar:
            tar.add(manifest_path, arcname=MANIFEST_NAME)
            tar.add(snapshot, arcname=BUNDLE_DB)
            for p in stored:  # tar streams the store files; no copy, no buffering
                tar.add(p, arcname=f"{ARTIFACTS_DIR}/{p.name}")

    return ExportReport(out, church_id, len(stored), missing, cells)


@dataclass
class ImportReport:
    church_id: str
    artifacts_restored: int
    missing_artifacts: list[str]
    external_paths: list[str] = field(default_factory=list)


def _bundle_member(tmp: Path, name: str) -> Path:
    """The extraction path of a manifest `files` entry; rejects anything outside
    the extraction directory (absolute paths, `..`)."""
    rel = PurePosixPath(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise GeorgError(f"bundle manifest names an unsafe path {name!r}")
    return tmp / rel


def import_bundle(config: GeorgConfig, bundle: Path) -> ImportReport:
    """Restore a `.georg` bundle into this config's empty data dir."""
    db_path = database_path(config.data_dir)
    if db_path.exists():
        raise GeorgError(f"{db_path} already exists; import needs an empty data dir")
    known = scripts()
    with tempfile.TemporaryDirectory() as work:
        tmp = Path(work)
        with tarfile.open(bundle) as tar:
            tar.extractall(tmp, filter="data")
        manifest = json.loads((tmp / MANIFEST_NAME).read_text())
        if manifest.get("format") != BUNDLE_FORMAT:
            raise GeorgError(f"{bundle}: unsupported bundle format {manifest.get('format')}")
        for name, sha in manifest["files"].items():
            path = _bundle_member(tmp, name)
            if not path.is_file() or _sha256(path) != sha:
                raise GeorgError(f"{bundle}: {name} is corrupt or missing")
        bundle_revision = manifest.get("alembic_revision")
        if bundle_revision is not None:
            try:
                known.get_revision(bundle_revision)
            except CommandError:
                raise GeorgError(
                    f"{bundle} was made by a newer Georg (database revision "
                    f"{bundle_revision!r}); import it with that version or newer"
                ) from None

        extracted_db = tmp / BUNDLE_DB
        new_dir = str(config.data_dir.resolve())
        remap = {DATA_DIR_TOKEN: new_dir}
        external: list[str] = []
        new_options = _option_paths(config.model_dump(mode="json"))
        for entry in manifest.get("paths", []):
            if entry.get("under_data_dir"):
                continue  # DATA_DIR_TOKEN covers everything below the old data dir
            # The same configured option on the new host wins (e.g. a moved
            # input root); without one, the recorded source path is kept.
            locator = entry.get("option")
            new_value = new_options.get(locator) if locator else None
            target = new_value if new_value else entry["path"]
            remap[entry["token"]] = target
            external.append(f"{entry['path']} -> {target}")
        _rewrite_paths(extracted_db, remap)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        extracted_db.replace(db_path)

        store = LocalStore.for_data_dir(config.data_dir)
        artifact_dir = tmp / ARTIFACTS_DIR
        restored = 0
        for p in artifact_dir.iterdir() if artifact_dir.is_dir() else []:
            if _sha256(p) != p.name:
                raise GeorgError(f"{bundle}: artifact {p.name} failed its checksum")
            dest = store.location(p.name)
            if not dest.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dest)
                dest.chmod(0o444)
            restored += 1

    upgrade(db_path)
    engine = create_engine(db_path)
    try:
        with session_factory(engine).begin() as session:
            # Queued work isn't carried over: the queue is re-derived on resume.
            for job in session.scalars(select(Job)).all():
                session.delete(job)
            session.execute(
                sa_update(StageRun)
                .where(StageRun.status.in_(("queued", "running")))
                .values(status="cancelled")
            )
            IdentityRepository(session).set_state("paused")
            AuditRepository(session).add(
                None,
                "import",
                manifest.get("instance"),
                detail={"church_id": manifest.get("church_id"), "bundle": bundle.name},
            )
    finally:
        engine.dispose()
    return ImportReport(
        church_id=str(manifest.get("church_id")),
        artifacts_restored=restored,
        missing_artifacts=list(manifest.get("missing_artifacts") or []),
        external_paths=external,
    )


@dataclass
class ResumeReport:
    state: str
    resumed: bool
    checked: int
    missing: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return len(self.missing) + len(self.unreachable) + len(self.unverifiable)


def _rederive_jobs(session: Session, queue: JobQueue) -> int:
    """Queue the work the bundle's dropped job table held: publish sermons whose
    publication never completed, and process services that were discovered but
    never produced a revision. Rows go through `session` — the queue's own
    connection can't begin while this transaction holds the write lock."""
    pending = session.scalars(
        select(Publication.sermon_id).where(
            Publication.status.in_(("pending", "publishing", "failed"))
        )
    ).all()
    unprocessed = session.scalars(
        select(Service.id).where(
            Service.purged_at.is_(None),  # retention retired it; don't start over
            Service.id.not_in(
                select(Sermon.service_id).where(Sermon.current_revision_id.is_not(None))
            ),
        )
    ).all()
    for sermon_id in pending:
        session.add(
            Job(kind=PUBLISH, payload={"sermon_id": sermon_id}, max_attempts=queue.max_attempts)
        )
    for service_id in unprocessed:
        session.add(
            Job(kind=PROCESS, payload={"service_id": service_id}, max_attempts=queue.max_attempts)
        )
    return len(pending) + len(unprocessed)


def resume(
    config: GeorgConfig,
    sessions: sessionmaker[Session],
    *,
    queue: JobQueue | None = None,
    force: bool = False,
    targets: Mapping[str, Any] | None = None,
) -> ResumeReport:
    """Unpause an imported instance after checking its live publications.

    Every publication the bundle says is published is checked with the target's
    `find_existing(key)`. Missing objects, unreachable targets, and publications
    whose target is no longer configured block the resume unless `force`.
    """
    with sessions.begin() as session:
        state = IdentityRepository(session).state()
        if state == "frozen":
            raise GeorgError("this instance was exported and is frozen; it can never resume")
        if state == "active":
            return ResumeReport(state=state, resumed=False, checked=0)
        stmt = select(Publication).where(
            Publication.status.in_(("published", "conflict")) | Publication.external_id.is_not(None)
        )
        publications = session.scalars(stmt).all()
        pubs = [(p.target_name, p.publication_key) for p in publications]

    configured = {o.name: o for o in config.outputs}
    report = ResumeReport(state=state, resumed=False, checked=len(pubs))
    for target_name, key in pubs:
        target = (targets or {}).get(target_name)
        if target is None:
            output_config = configured.get(target_name)
            if output_config is None:
                report.unverifiable.append(f"{target_name}: {key} (target not configured)")
                continue
            target = build_output(output_config, timezone=config.timezone)
        try:
            existing = target.find_existing(key)
        except Exception as exc:
            report.unreachable.append(f"{target_name}: {key} ({exc})")
            continue
        if existing is None:
            report.missing.append(f"{target_name}: {key}")
    if report.problems and not force:
        return report
    enqueued = 0
    with sessions.begin() as session:
        # One transaction: an interrupted resume must not activate the instance
        # without also restoring its queue.
        IdentityRepository(session).set_state("active")
        if queue is not None:
            enqueued = _rederive_jobs(session, queue)
    if enqueued:
        log.info("re-derived %d pending job(s)", enqueued)
    report.resumed = True
    return report
