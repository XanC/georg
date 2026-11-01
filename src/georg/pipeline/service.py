# SPDX-License-Identifier: AGPL-3.0-only
"""Discovery and processing of services recorded in the database."""

import logging
import mimetypes
import shutil
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from georg.core.config import GeorgConfig, NamedModuleConfig
from georg.core.contracts import OutputTarget
from georg.core.errors import ModuleError
from georg.core.types import ArtifactRef, PublicationRequest, ServiceBoundaries, SourceItem
from georg.db.models import Artifact
from georg.db.repositories import (
    ArtifactRepository,
    PublicationRepository,
    SermonRepository,
    ServiceRepository,
)
from georg.jobs.queue import JobQueue
from georg.media import ffmpeg
from georg.pipeline.local import LocalRunResult, StageCache, run_local
from georg.pipeline.registry import Processors, build_input, build_output
from georg.storage.local import LocalStore

log = logging.getLogger(__name__)

INPUT_HINT = "input"
"""`Service.hints` key naming the configured input that discovered it."""
PROCESS = "process"
DISCOVER = "discover"
RENDER = "render"
PUBLISH = "publish"


def discover(
    config: GeorgConfig,
    sessions: sessionmaker[Session],
    queue: JobQueue,
    *,
    since: datetime | None = None,
) -> list[int]:
    """Record new recordings from every input and queue them; return their service IDs."""
    new_ids = []
    for input_config in config.inputs:
        source = build_input(input_config)
        for item in source.discover(since):
            item = item.model_copy(update={"hints": {**item.hints, INPUT_HINT: input_config.name}})
            with sessions.begin() as session:
                service, new = ServiceRepository(session).add_discovered(item)
                service_id = service.id
            if new:
                queue.enqueue(PROCESS, {"service_id": service_id})
                new_ids.append(service_id)
                log.info("discovered service %d: %s", service_id, item.title or item.source_ref)
    return new_ids


def _input_config(config: GeorgConfig, name: object) -> NamedModuleConfig:
    for input_config in config.inputs:
        if input_config.name == name:
            return input_config
    raise ModuleError(f"no configured input named {name!r}")


def process_service(
    config: GeorgConfig,
    sessions: sessionmaker[Session],
    service_id: int,
    processors: Processors,
    *,
    store: LocalStore | None = None,
    cache: StageCache | None = None,
) -> int | None:
    """Fetch, analyse and render a service, then propose its first sermon revision.

    Returns the new revision's ID, or None if the sermon already has revisions
    (e.g. a retry after the proposal was saved), in which case nothing changes.
    """
    store = store or LocalStore.for_data_dir(config.data_dir)
    cache = cache or StageCache(config.data_dir / "cache")
    with sessions() as session:
        repo = ServiceRepository(session)
        service = repo.get(service_id)
        item = repo.to_source_item(service)
        included = tuple(
            part for part, row in zip(item.parts, service.parts, strict=True) if row.included
        )
        input_name = service.hints.get(INPUT_HINT)
        existing = SermonRepository(session).for_service(service_id)
        if any(sermon.current_revision_id for sermon in existing):
            return None
    if not included:
        raise ModuleError(f"service {service_id} has no included recording parts")

    result, out_dir = _run(config, service_id, item, included, input_name, processors, cache)
    with sessions.begin() as session:
        sermon_audio_id = _store_files(session, store, config, service_id, result, out_dir)
        sermons = SermonRepository(session)
        found = sermons.for_service(service_id)
        sermon = found[0] if found else sermons.create(service_id)
        revision = sermons.add_revision(
            sermon.id,
            author="system",
            boundaries=result.boundaries,
            metadata=result.metadata,
            transcript_markdown=result.files["transcript"].read_text(encoding="utf-8"),
            field_origins={"boundaries": processors.detector.version},
            sermon_audio_artifact_id=sermon_audio_id,
        )
        service = ServiceRepository(session).get(service_id)
        service.duration_ms = result.duration_ms
        service.status_summary = "awaiting review"
        revision_id = revision.id
    shutil.rmtree(out_dir, ignore_errors=True)
    log.info("service %d: proposed revision %d (%s)", service_id, revision_id, result.timings)
    return revision_id


def render_revision(
    config: GeorgConfig,
    sessions: sessionmaker[Session],
    revision_id: int,
    processors: Processors,
    *,
    store: LocalStore | None = None,
    cache: StageCache | None = None,
) -> None:
    """Render a (reviewer-edited) revision's boundaries into new renditions.

    Analysis comes from the stage cache; the formatter is not run again.
    """
    store = store or LocalStore.for_data_dir(config.data_dir)
    cache = cache or StageCache(config.data_dir / "cache")
    with sessions() as session:
        sermons = SermonRepository(session)
        revision = sermons.revision(revision_id)
        boundaries = sermons.boundaries(revision)
        service_id = sermons.get(revision.sermon_id).service_id
        if _renditions_for(session, service_id, boundaries):
            return
        repo = ServiceRepository(session)
        service = repo.get(service_id)
        item = repo.to_source_item(service)
        included = tuple(
            part for part, row in zip(item.parts, service.parts, strict=True) if row.included
        )
        input_name = service.hints.get(INPUT_HINT)
    result, out_dir = _run(
        config,
        service_id,
        item,
        included,
        input_name,
        replace(processors, formatter=None),
        cache,
        boundaries=boundaries,
    )
    with sessions.begin() as session:
        sermon_audio_id = _store_files(
            session, store, config, service_id, result, out_dir, transcript=False
        )
        sermons = SermonRepository(session)
        if sermon_audio_id and sermons.revision(revision_id).sermon_audio_artifact_id is None:
            sermons.attach_sermon_audio(revision_id, sermon_audio_id)
    shutil.rmtree(out_dir, ignore_errors=True)
    log.info("revision %d: rendered (%s)", revision_id, result.timings)


def _run(
    config: GeorgConfig,
    service_id: int,
    item: SourceItem,
    included: tuple[object, ...],
    input_name: object,
    processors: Processors,
    cache: StageCache,
    *,
    boundaries: ServiceBoundaries | None = None,
) -> tuple[LocalRunResult, Path]:
    if not included:
        raise ModuleError(f"service {service_id} has no included recording parts")
    work = config.data_dir / "work" / str(service_id)
    item = item.model_copy(update={"parts": included})
    parts = build_input(_input_config(config, input_name)).fetch(item, work / "parts")
    out_dir = work / "out"
    shutil.rmtree(out_dir, ignore_errors=True)
    result = run_local(
        parts,
        out_dir,
        processors,
        cache=cache,
        renditions=config.renditions,
        language=config.language,
        source=item,
        boundaries=boundaries,
    )
    return result, out_dir


def _bounds_key(b: ServiceBoundaries) -> list[int]:
    return [b.service_start_ms, b.sermon_start_ms, b.sermon_end_ms, b.service_end_ms]


def _store_files(
    session: Session,
    store: LocalStore,
    config: GeorgConfig,
    service_id: int,
    result: LocalRunResult,
    out_dir: Path,
    *,
    transcript: bool = True,
) -> int | None:
    """Store a run's files as artifacts; return the sermon audio's artifact ID."""
    spans = {spec.name: spec.span for spec in config.renditions}
    files = [(n, p) for n, p in result.files.items() if transcript or n != "transcript"]
    if transcript:
        files.append(("report", out_dir / "report.json"))
    artifacts = ArtifactRepository(session)
    sermon_audio_id = None
    for name, path in files:
        ref = store.put(path, mime=mimetypes.guess_type(path.name)[0] or "application/json")
        meta: dict[str, object] = {"name": name}
        if name == "transcript":
            kind = "transcript_formatted" if result.formatted else "transcript_raw"
        elif name == "report":
            kind = "boundary_proposal"
        else:
            kind = "sermon_audio" if spans.get(name) == "sermon" else "rendition"
            ref = ref.model_copy(update={"duration_ms": ffmpeg.probe(path).duration_ms})
            meta["boundaries"] = _bounds_key(result.boundaries)
        artifact = artifacts.add(service_id, kind, ref, storage_key=ref.sha256, meta=meta)
        if kind == "sermon_audio" and sermon_audio_id is None:
            sermon_audio_id = artifact.id
    return sermon_audio_id


def _renditions_for(
    session: Session, service_id: int, boundaries: ServiceBoundaries
) -> dict[str, Artifact]:
    """The newest stored rendition of each name cut at exactly these boundaries."""
    key = _bounds_key(boundaries)
    found: dict[str, Artifact] = {}
    for artifact in ArtifactRepository(session).for_service(service_id):
        if (
            artifact.kind in ("rendition", "sermon_audio")
            and artifact.meta.get("boundaries") == key
        ):
            name = str(artifact.meta.get("name"))
            if name not in found or artifact.id > found[name].id:
                found[name] = artifact
    return found


def publication_key(config: GeorgConfig, sermon_id: int, target_name: str) -> str:
    return f"georg:{config.instance}:sermon-{sermon_id}:{target_name}"


def publish_sermon(
    config: GeorgConfig,
    sessions: sessionmaker[Session],
    sermon_id: int,
    *,
    store: LocalStore | None = None,
    outputs: Sequence[OutputTarget] | None = None,
) -> dict[str, str]:
    """Publish the sermon's current revision to every output; return status by output.

    Outputs already holding this revision are skipped. Each output is attempted even
    if another fails; any failure is raised afterwards so the job is retried.
    """
    store = store or LocalStore.for_data_dir(config.data_dir)
    targets = (
        list(outputs)
        if outputs is not None
        else [build_output(c, timezone=config.timezone) for c in config.outputs]
    )
    with sessions() as session:
        sermons = SermonRepository(session)
        sermon = sermons.get(sermon_id)
        revision = sermons.current_revision(sermon_id)
        if revision is None:
            raise ModuleError(f"sermon {sermon_id} has no revision")
        if config.review.require_approval and not sermons.is_approved(sermon_id):
            raise ModuleError(f"sermon {sermon_id}'s current revision is not approved")
        metadata = sermons.metadata(revision)
        service = ServiceRepository(session).get(sermon.service_id)
        service_date = metadata.service_date or service.service_date
        renditions = _renditions_for(session, service.id, sermons.boundaries(revision))
        transcript = revision.transcript_markdown
        revision_id = revision.id
        files = {
            name: ArtifactRef(
                path=store.path(a.sha256),
                sha256=a.sha256,
                size=a.size,
                mime=a.mime,
                duration_ms=a.duration_ms,
            )
            for name, a in renditions.items()
        }
    if service_date is None:
        raise ModuleError(f"sermon {sermon_id} has no service date")

    statuses: dict[str, str] = {}
    errors: list[ModuleError] = []
    for target in targets:
        missing = target.renditions - files.keys()
        if missing:
            errors.append(
                ModuleError(f"{target.name}: not rendered yet: {sorted(missing)}", retryable=True)
            )
            statuses[target.name] = "waiting"
            continue
        key = publication_key(config, sermon_id, target.name)
        with sessions.begin() as session:
            pubs = PublicationRepository(session)
            previous = pubs.find(sermon_id, target.name)
            if previous and previous.status == "published" and previous.revision_id == revision_id:
                statuses[target.name] = "published"
                continue
            publication = pubs.prepare(sermon_id, target.name, revision_id, key)
            publication_id = publication.id
            key = publication.publication_key
            existing = pubs.external_ref(publication)
            pubs.mark_publishing(publication_id)
        try:
            existing = existing or target.find_existing(key)
            ref = target.publish(
                PublicationRequest(
                    publication_key=key,
                    service_date=service_date,
                    metadata=metadata,
                    transcript_markdown=transcript if target.wants_transcript else None,
                    files={n: files[n] for n in target.renditions},
                    existing=existing,
                )
            )
        except Exception as exc:
            error = exc if isinstance(exc, ModuleError) else ModuleError(str(exc))
            errors.append(ModuleError(f"{target.name}: {error}", retryable=error.retryable))
            statuses[target.name] = "failed"
            with sessions.begin() as session:
                PublicationRepository(session).mark_failed(publication_id, str(exc))
            continue
        with sessions.begin() as session:
            PublicationRepository(session).mark_published(publication_id, ref)
        statuses[target.name] = "published"
        log.info(
            "sermon %d: published to %s: %s", sermon_id, target.name, ref.url or ref.external_id
        )
    if errors:
        raise ModuleError(
            "; ".join(str(e) for e in errors), retryable=any(e.retryable for e in errors)
        )
    return statuses
