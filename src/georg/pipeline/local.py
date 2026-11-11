# SPDX-License-Identifier: AGPL-3.0-only
"""Run the processing stages over local recording files, without the database.

Used to evaluate Georg on real recordings and as the building block for the
queued pipeline. Expensive stage results (audio extraction, classification,
transcription, formatting) are cached by input and module version, so re-running
with different boundaries or detector parameters is cheap.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Final

from georg.core.config import RenditionSpec
from georg.core.contracts import ExplainingMetadataGenerator
from georg.core.types import (
    AudioSegment,
    BoundaryProposal,
    FormatContext,
    FormattedTranscript,
    MediaInput,
    MetadataContext,
    RemoteMedia,
    SermonMetadata,
    ServiceBoundaries,
    SourceItem,
    SourcePart,
    TimeRange,
    Transcript,
)
from georg.media import ffmpeg, mastering
from georg.modules.processors.classify import segments_from_json, segments_to_json
from georg.pipeline.registry import Processors

log = logging.getLogger(__name__)

EXTRACT_VERSION = "extract-1"
PREVIEW = "preview"
"""Built-in rendition of the whole extracted recording, for the review page's player."""
PREVIEW_VERSION = "preview-1"
"""Bump whenever the preview's encoding changes."""
PREVIEW_SPEC = RenditionSpec(
    name=PREVIEW, span="service", channels=1, sample_rate=22050, bitrate_kbps=40
)
"""Encoding only (CBR so browsers seek accurately); never cut, so `span` is unused."""
CUT_PREVIEWS: Final = {"sermon": "sermon_preview", "service": "service_preview"}
"""Built-in low-quality copy of each span's cut, by span; for the review page only."""
_LAME_VBR_KBPS: Final = (245, 225, 190, 175, 165, 130, 115, 100, 85, 65)


def _approx_kbps(spec: RenditionSpec) -> tuple[int, int, int]:
    kbps = spec.bitrate_kbps if spec.vbr_quality is None else _LAME_VBR_KBPS[spec.vbr_quality]
    return kbps or 0, spec.channels, spec.sample_rate


def cut_preview_sources(renditions: Sequence[RenditionSpec]) -> dict[str, str]:
    """Each cut preview's name -> the highest-bitrate rendition of its span it is encoded from."""
    best: dict[str, RenditionSpec] = {}
    for spec in renditions:
        if spec.span not in best or _approx_kbps(spec) > _approx_kbps(best[spec.span]):
            best[spec.span] = spec
    return {CUT_PREVIEWS[span]: spec.name for span, spec in best.items()}


class StageCache:
    """Stage outputs stored as `<root>/<stage>/<key><suffix>`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def key(*parts: object) -> str:
        return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:32]

    def path(self, stage: str, key: str, suffix: str = ".json") -> Path:
        return self.root / stage / f"{key}{suffix}"

    def text(self, stage: str, key: str, compute: Callable[[], str]) -> tuple[str, bool]:
        """Return the cached text, computing and storing it on a miss; and whether it hit."""
        path = self.path(stage, key)
        if path.exists():
            return path.read_text(encoding="utf-8"), True
        value = compute()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.partial")
        tmp.write_text(value, encoding="utf-8")
        tmp.replace(path)
        return value, False


@dataclass(frozen=True)
class LocalRunResult:
    proposal: BoundaryProposal
    boundaries: ServiceBoundaries
    """What was rendered: the proposal, or the caller's override."""
    metadata: SermonMetadata
    formatted: FormattedTranscript | None
    files: dict[str, Path]
    duration_ms: int
    """Length of the joined recording."""
    timings: dict[str, float] = field(default_factory=dict)
    """Seconds per stage; 0 for cache hits."""
    transcript: Transcript | None = None
    """The full-service transcript used by detection and included in the report."""
    metadata_origins: dict[str, str] = field(default_factory=dict)
    """Where derived metadata fields came from (`MetadataProposal.origins`)."""


def _source_key(parts: Sequence[MediaInput]) -> str:
    stats = [
        p.key
        if isinstance(p, RemoteMedia)
        else (p.resolve(), p.stat().st_size, p.stat().st_mtime_ns)
        for p in parts
    ]
    return StageCache.key(EXTRACT_VERSION, *stats)


def _plain_markdown(transcript: Transcript) -> str:
    return "\n\n".join(s.text.strip() for s in transcript.segments if s.text.strip()) + "\n"


def run_local(
    parts: Sequence[MediaInput],
    out_dir: Path,
    processors: Processors,
    *,
    cache: StageCache,
    renditions: Sequence[RenditionSpec],
    language: str | None = None,
    service_date: date | None = None,
    documents: Sequence[str] = (),
    document_texts: Mapping[str, str] | None = None,
    source: SourceItem | None = None,
    defaults: SermonMetadata | None = None,
    metadata_override: SermonMetadata | None = None,
    boundaries: ServiceBoundaries | None = None,
    transcribe_sermon: bool = True,
    format_transcript: bool = True,
    mastering_params: mastering.MasteringParams | None = None,
) -> LocalRunResult:
    """Process one recording (its parts in order) into `out_dir`.

    Writes the renditions (`<name>.mp3`), `transcript.md` (the sermon), and
    `report.json` (boundaries, explanation, metadata, timings). Pass
    `boundaries` to render reviewed timestamps instead of the detector's, and
    `source` to give the metadata generator the discovered item (otherwise one is
    made from the files, `service_date` and `documents`). `document_texts` (plain text
    by document name) is passed to a metadata generator that `uses_documents`.
    """
    p = processors
    timings: dict[str, float] = {}
    out_dir.mkdir(parents=True, exist_ok=True)

    def timed[T](stage: str, fn: Callable[[], tuple[T, bool]]) -> T:
        start = time.monotonic()
        value, hit = fn()
        timings[stage] = 0.0 if hit else round(time.monotonic() - start, 1)
        return value

    source_key = _source_key(parts)
    audio = cache.path("audio", source_key, ".flac")

    def probe_remote(part: RemoteMedia) -> str:
        """Probe a remote part once, cached by its content key across attempts."""
        return cache.text(
            "probe",
            cache.key(part.key),
            lambda: json.dumps(asdict(ffmpeg.probe_media(part, retryable=True))),
        )[0]

    def remote_probes() -> dict[str, ffmpeg.MediaInfo]:
        return {
            part.key: ffmpeg.MediaInfo(**json.loads(probe_remote(part)))
            for part in parts
            if isinstance(part, RemoteMedia)
        }

    def extract() -> tuple[Path, bool]:
        if audio.exists():
            return audio, True
        audio.parent.mkdir(parents=True, exist_ok=True)
        return ffmpeg.extract_audio(list(parts), audio, remote_probes=remote_probes()), False

    timed("extract", extract)
    duration_ms = ffmpeg.probe(audio).duration_ms
    preview = timed("preview", lambda: _preview(cache, source_key, audio, duration_ms))

    segments: list[AudioSegment] = timed(
        "classify",
        lambda: _parse(
            cache.text(
                "classify",
                cache.key(source_key, p.classifier.version),
                lambda: segments_to_json(p.classifier.classify(audio)),
            ),
            segments_from_json,
        ),
    )
    transcript: Transcript = timed(
        "transcribe",
        lambda: _parse(
            cache.text(
                "transcribe",
                cache.key(source_key, p.transcriber.version, language),
                lambda: p.transcriber.transcribe(audio, language).model_dump_json(),
            ),
            Transcript.model_validate_json,
        ),
    )

    start = time.monotonic()
    proposal = p.detector.detect(segments, duration_ms, transcript)
    timings["detect"] = round(time.monotonic() - start, 1)
    chosen = boundaries or proposal.boundaries
    if transcribe_sermon:
        sermon_processor = p.sermon_transcriber or p.transcriber
        cut_start = max(chosen.sermon_start_ms - p.sermon_transcribe_pad_ms, 0)
        cut_end = min(chosen.sermon_end_ms + p.sermon_transcribe_pad_ms, duration_ms)
        cut_span = TimeRange(start_ms=cut_start, end_ms=max(cut_end, cut_start + 1))
        sermon_key = cache.key(
            source_key,
            sermon_processor.version,
            language,
            cut_span.start_ms,
            cut_span.end_ms,
        )
        sermon_audio = out_dir / ".sermon-transcribe.flac"

        def sermon_transcribe() -> tuple[Transcript, bool]:
            def compute() -> str:
                ffmpeg.cut(audio, cut_span, sermon_audio)
                try:
                    return sermon_processor.transcribe(sermon_audio, language).model_dump_json()
                finally:
                    sermon_audio.unlink(missing_ok=True)

            return _parse(
                cache.text("sermon_transcribe", sermon_key, compute),
                Transcript.model_validate_json,
            )

        try:
            sermon_transcript_padded = timed("sermon_transcribe", sermon_transcribe)
            sermon_transcript = _shift_transcript(
                sermon_transcript_padded, cut_span.start_ms
            ).within(chosen.sermon)
        except Exception as exc:
            log.warning(
                "sermon transcription failed for %s-%s; using full-service transcript slice: %s",
                chosen.sermon_start_ms,
                chosen.sermon_end_ms,
                exc,
            )
            sermon_transcript = transcript.within(chosen.sermon)
    else:
        sermon_transcript = transcript.within(chosen.sermon)

    service_date = service_date or (source.service_date if source else None)
    source = source or SourceItem(
        source_type="local",
        source_ref=source_key,
        parts=tuple(SourcePart(ref=str(part), name=part.name) for part in parts),
        service_date=service_date,
        hints={"documents": list(documents)},
    )
    defaults = defaults or SermonMetadata(service_date=service_date)
    origins: dict[str, str] = {}
    if metadata_override is not None:
        metadata = metadata_override
    else:
        start = time.monotonic()
        meta_ctx = MetadataContext(
            source=source,
            transcript=sermon_transcript,
            defaults=defaults,
            documents=dict(document_texts or {}),
        )
        if isinstance(p.metadata, ExplainingMetadataGenerator):
            meta_proposal = p.metadata.propose(meta_ctx)
            metadata, origins = meta_proposal.metadata, dict(meta_proposal.origins)
        else:
            metadata = p.metadata.generate(meta_ctx)
        timings["metadata"] = round(time.monotonic() - start, 1)

    formatted: FormattedTranscript | None = None
    if format_transcript and p.formatter is not None:
        formatter = p.formatter
        ctx = FormatContext(language=language or transcript.language, metadata=metadata)
        formatted = timed(
            "format",
            lambda: _parse(
                cache.text(
                    "format",
                    cache.key(sermon_transcript.model_dump_json(), formatter.version, ctx),
                    lambda: formatter.format(sermon_transcript, ctx).model_dump_json(),
                ),
                FormattedTranscript.model_validate_json,
            ),
        )
    transcript_md = out_dir / "transcript.md"
    transcript_md.write_text(
        formatted.markdown if formatted else _plain_markdown(sermon_transcript), encoding="utf-8"
    )

    start = time.monotonic()
    service_cut = ffmpeg.cut(audio, chosen.service, out_dir / ".service.flac")
    mastered = out_dir / ".service.mastered.flac"
    try:
        mastering.master(service_cut, mastered, mastering_params)
        refs = mastering.render(
            mastered,
            chosen,
            renditions,
            out_dir,
            origin_ms=chosen.service_start_ms,
            tags=_tags(metadata),
        )
    finally:
        service_cut.unlink(missing_ok=True)
        mastered.unlink(missing_ok=True)
    timings["master"] = round(time.monotonic() - start, 1)

    files = {PREVIEW: preview, **{name: ref.path for name, ref in refs.items()}}
    start = time.monotonic()
    for name, rendition in cut_preview_sources(renditions).items():
        files[name] = ffmpeg.encode_mp3(refs[rendition].path, out_dir / f"{name}.mp3", PREVIEW_SPEC)
    timings["cut_previews"] = round(time.monotonic() - start, 1)
    files["transcript"] = transcript_md
    result = LocalRunResult(
        proposal, chosen, metadata, formatted, files, duration_ms, timings, transcript, origins
    )
    _write_report(out_dir / "report.json", result)
    return result


def _shift_transcript(transcript: Transcript, offset_ms: int) -> Transcript:
    def shift_range(span: TimeRange) -> TimeRange:
        return TimeRange(
            start_ms=span.start_ms + offset_ms,
            end_ms=span.end_ms + offset_ms,
        )

    segments = tuple(
        segment.model_copy(
            update={
                "range": shift_range(segment.range),
                "words": tuple(
                    word.model_copy(update={"range": shift_range(word.range)})
                    for word in segment.words
                ),
            }
        )
        for segment in transcript.segments
    )
    return transcript.model_copy(update={"segments": segments})


def _preview(
    cache: StageCache, source_key: str, audio: Path, duration_ms: int
) -> tuple[Path, bool]:
    """Encode the whole recording once per source; boundaries never change it."""
    path = cache.path(PREVIEW, cache.key(source_key, PREVIEW_VERSION), ".mp3")
    if path.exists():
        return path, True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.partial.mp3")
    try:
        ffmpeg.encode_mp3(audio, tmp, PREVIEW_SPEC)
        ffmpeg.verify_decode(tmp, expected_ms=duration_ms)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)
    return path, False


def _parse[T](cached: tuple[str, bool], parse: Callable[[str], T]) -> tuple[T, bool]:
    text, hit = cached
    return parse(text), hit


def _tags(metadata: SermonMetadata) -> dict[str, str]:
    tags = {
        "title": metadata.title or metadata.occasion,
        "artist": metadata.preacher,
        "date": metadata.service_date.isoformat() if metadata.service_date else None,
    }
    return {k: v for k, v in tags.items() if v}


def _write_report(path: Path, result: LocalRunResult) -> None:
    report = {
        "proposal": result.proposal.model_dump(mode="json"),
        "boundaries": result.boundaries.model_dump(mode="json"),
        "transcript": result.transcript.model_dump(mode="json") if result.transcript else None,
        "metadata": result.metadata.model_dump(mode="json"),
        "metadata_origins": result.metadata_origins,
        "words_preserved": result.formatted.words_preserved if result.formatted else None,
        "discrepancies": list(result.formatted.discrepancies) if result.formatted else [],
        "files": {k: str(v) for k, v in result.files.items()},
        "timings_s": result.timings,
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
