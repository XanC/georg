# SPDX-License-Identifier: AGPL-3.0-only
"""Discovery and processing of services recorded in the database."""

import logging
import mimetypes
import shutil
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from georg.core.config import GeorgConfig, NamedModuleConfig
from georg.core.errors import ModuleError
from georg.db.repositories import ArtifactRepository, SermonRepository, ServiceRepository
from georg.jobs.queue import JobQueue
from georg.pipeline.local import StageCache, run_local
from georg.pipeline.registry import Processors, build_input
from georg.storage.local import LocalStore

log = logging.getLogger(__name__)

INPUT_HINT = "input"
"""`Service.hints` key naming the configured input that discovered it."""
PROCESS = "process"
DISCOVER = "discover"


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

    work = config.data_dir / "work" / str(service_id)
    item = item.model_copy(update={"parts": included})
    parts = build_input(_input_config(config, input_name)).fetch(item, work / "parts")
    out_dir = work / "out"
    result = run_local(
        parts,
        out_dir,
        processors,
        cache=cache,
        renditions=config.renditions,
        language=config.language,
        source=item,
    )

    spans = {spec.name: spec.span for spec in config.renditions}
    with sessions.begin() as session:
        artifacts = ArtifactRepository(session)
        sermon_audio_id = None
        for name, path in [*result.files.items(), ("report", out_dir / "report.json")]:
            ref = store.put(path, mime=mimetypes.guess_type(path.name)[0] or "application/json")
            if name == "transcript":
                kind = "transcript_formatted" if result.formatted else "transcript_raw"
            elif name == "report":
                kind = "boundary_proposal"
            else:
                kind = "sermon_audio" if spans.get(name) == "sermon" else "rendition"
            artifact = artifacts.add(
                service_id, kind, ref, storage_key=ref.sha256, meta={"name": name}
            )
            if kind == "sermon_audio" and sermon_audio_id is None:
                sermon_audio_id = artifact.id

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
