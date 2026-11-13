# SPDX-License-Identifier: AGPL-3.0-only
"""Retention cleanup: Georg is not an archive.

A service's stored files are deleted `[retention] days` after its sermon was last
published through Georg, or after it was discovered if it never was. The database keeps
what Georg needs later: the service and its source identity (so discovery never fetches
it again), sermon revisions, and publications with their external IDs.
"""

import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from georg.core.config import GeorgConfig
from georg.db.models import Artifact, Publication, Sermon, SermonRevision, Service, StageRun
from georg.db.repositories import AuditRepository
from georg.db.types import utcnow
from georg.jobs.queue import JobQueue
from georg.storage.local import LocalStore

log = logging.getLogger(__name__)

CLEANUP: Final = "cleanup"
"""Job kind; enqueued at most once a day by discovery."""
ACTIVE: Final = ("queued", "running")


@dataclass
class Purge:
    service_id: int
    title: str
    reason: str
    """"published" (days since the last publication) or "discovered"."""
    since: datetime
    files: list[Path] = field(default_factory=list)
    bytes: int = 0


@dataclass
class CleanupReport:
    days: int
    purges: list[Purge] = field(default_factory=list)
    skipped_busy: list[int] = field(default_factory=list)
    """Due services left alone because they have queued or running jobs."""
    stale_cache: list[Path] = field(default_factory=list)
    stale_cache_bytes: int = 0

    @property
    def bytes(self) -> int:
        return sum(p.bytes for p in self.purges) + self.stale_cache_bytes


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _busy_services(session: Session, queue: JobQueue) -> set[int]:
    """Services with a queued or running job, found through the job's payload."""
    jobs = [j for status in ACTIVE for j in queue.list(status=status)]
    sermon_service = dict(session.execute(select(Sermon.id, Sermon.service_id)).all())
    revision_service = dict(
        session.execute(
            select(SermonRevision.id, Sermon.service_id).join(
                Sermon, Sermon.id == SermonRevision.sermon_id
            )
        ).all()
    )
    busy: set[int] = set()
    for job in jobs:
        payload = job.payload
        for key, mapping in (
            ("service_id", None),
            ("sermon_id", sermon_service),
            ("revision_id", revision_service),
        ):
            value = payload.get(key)
            if not isinstance(value, int):
                continue
            service_id = value if mapping is None else mapping.get(value)
            if service_id is not None:
                busy.add(service_id)
    return busy


def due(session: Session, days: int, now: datetime) -> list[tuple[Service, str, datetime]]:
    """Unpurged services past retention, with the clock that made them due."""
    last_published = dict(
        session.execute(
            select(Sermon.service_id, func.max(Publication.published_at))
            .join(Publication, Publication.sermon_id == Sermon.id)
            .where(Publication.published_at.is_not(None))
            .group_by(Sermon.service_id)
        ).all()
    )
    cutoff = now - timedelta(days=days)
    found = []
    for service in session.scalars(
        select(Service).where(Service.purged_at.is_(None)).order_by(Service.id)
    ):
        published = last_published.get(service.id)
        reason, since = (
            ("published", published) if published else ("discovered", service.discovered_at)
        )
        if since <= cutoff:
            found.append((service, reason, since))
    return found


def cleanup(
    config: GeorgConfig,
    sessions: sessionmaker[Session],
    queue: JobQueue,
    *,
    store: LocalStore | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> CleanupReport:
    """Delete the stored files of every service past retention (or list them)."""
    days = config.retention.days
    report = CleanupReport(days)
    if days <= 0:
        return report
    now = now or utcnow()
    store = store or LocalStore.for_data_dir(config.data_dir)
    cache_root = config.data_dir / "cache"
    with sessions() as session:
        busy = _busy_services(session, queue)
        candidates = [
            (service.id, service.title or service.source_ref, reason, since)
            for service, reason, since in due(session, days, now)
        ]
    purged: set[int] = set()
    for service_id, title, reason, since in candidates:
        if service_id in busy:
            report.skipped_busy.append(service_id)
            continue
        purge = Purge(service_id, title, reason, since)
        with sessions.begin() as session:
            keep_sha, keep_cache = _still_needed(session, service_id, purged)
            service = session.get(Service, service_id)
            assert service is not None
            shas = {
                a.sha256
                for a in session.scalars(select(Artifact).where(Artifact.service_id == service_id))
            }
            files = [store.location(sha) for sha in sorted(shas - keep_sha)]
            files += [
                cache_root / entry for entry in sorted(set(service.cache_entries) - keep_cache)
            ]
            work = config.data_dir / "work" / str(service_id)
            purge.files = [path for path in files if path.is_file()]
            purge.bytes = sum(_size(path) for path in purge.files) + _tree_size(work)
            if work.exists():
                purge.files.append(work)
            if not dry_run:
                _forget_files(session, service, now)
                AuditRepository(session).add(
                    None,
                    "service.purge",
                    f"service {service_id}",
                    {
                        "days": days,
                        "reason": reason,
                        "files": len(purge.files),
                        "bytes": purge.bytes,
                    },
                )
        if not dry_run:
            _delete(purge.files)
            log.info(
                "service %d: removed %d files (%d bytes)", service_id, len(purge.files), purge.bytes
            )
        purged.add(service_id)
        report.purges.append(purge)
    _stale_cache(sessions, cache_root, now - timedelta(days=days), purged, report)
    if not dry_run:
        _delete(report.stale_cache)
    return report


def _still_needed(session: Session, service_id: int, purged: set[int]) -> tuple[set[str], set[str]]:
    """Artifact hashes and cache entries another unpurged service still uses."""
    others = select(Service.id).where(
        Service.purged_at.is_(None), Service.id != service_id, Service.id.not_in(purged)
    )
    shas = set(session.scalars(select(Artifact.sha256).where(Artifact.service_id.in_(others))))
    entries = {
        entry
        for (cache_entries,) in session.execute(
            select(Service.cache_entries).where(Service.id.in_(others))
        )
        for entry in cache_entries
    }
    return shas, entries


def _forget_files(session: Session, service: Service, now: datetime) -> None:
    """Drop the rows that point at deleted files; keep artifacts revisions refer to."""
    referenced = select(SermonRevision.sermon_audio_artifact_id).where(
        SermonRevision.sermon_audio_artifact_id.is_not(None)
    )
    session.execute(
        delete(Artifact).where(Artifact.service_id == service.id, Artifact.id.not_in(referenced))
    )
    session.execute(delete(StageRun).where(StageRun.service_id == service.id))
    service.cache_entries = []
    service.purged_at = now


def _stale_cache(
    sessions: sessionmaker[Session],
    cache_root: Path,
    cutoff: datetime,
    purged: set[int],
    report: CleanupReport,
) -> None:
    """Cache files no unpurged service records and untouched since `cutoff`: left from
    services processed before Georg recorded cache use, or from `georg run`."""
    if not cache_root.is_dir():
        return
    with sessions() as session:
        owned = {
            entry
            for (entries,) in session.execute(
                select(Service.cache_entries).where(
                    Service.purged_at.is_(None), Service.id.not_in(purged)
                )
            )
            for entry in entries
        }
    limit = cutoff.timestamp()
    for path in sorted(cache_root.rglob("*")):
        if not path.is_file() or str(path.relative_to(cache_root)) in owned:
            continue
        if path.stat().st_mtime <= limit:
            report.stale_cache.append(path)
            report.stale_cache_bytes += _size(path)


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(_size(p) for p in path.rglob("*") if p.is_file())


def _delete(paths: Iterable[Path]) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def maybe_enqueue_cleanup(
    config: GeorgConfig, queue: JobQueue, now: datetime | None = None
) -> int | None:
    """Queue a cleanup job unless retention is off or one was queued in the last day."""
    if config.retention.days <= 0:
        return None
    now = now or utcnow()
    last = queue.last_created_at(CLEANUP)
    if last is not None and now - last < timedelta(days=1):
        return None
    return queue.enqueue(CLEANUP, {})
