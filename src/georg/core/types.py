# SPDX-License-Identifier: AGPL-3.0-only
"""Value types exchanged between the core and modules.

All times are integer milliseconds from the start of the service's source timeline
(the concatenation of the source parts, in order). Ranges are half-open: [start, end).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TimeRange(Model):
    start_ms: int = Field(ge=0)
    end_ms: int

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def contains(self, other: "TimeRange") -> bool:
        return self.start_ms <= other.start_ms and other.end_ms <= self.end_ms


SegmentLabel = Literal["speech", "music", "noise", "silence"]


class AudioSegment(Model):
    """A stretch of audio with one dominant class, as produced by an `AudioClassifier`."""

    range: TimeRange
    label: SegmentLabel
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ServiceBoundaries(Model):
    """The four timestamps that define a service and its sermon."""

    service_start_ms: int = Field(ge=0)
    sermon_start_ms: int
    sermon_end_ms: int
    service_end_ms: int

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        s = self
        if not (s.service_start_ms <= s.sermon_start_ms < s.sermon_end_ms <= s.service_end_ms):
            raise ValueError("require service_start <= sermon_start < sermon_end <= service_end")
        return self

    @property
    def service(self) -> TimeRange:
        return TimeRange(start_ms=self.service_start_ms, end_ms=self.service_end_ms)

    @property
    def sermon(self) -> TimeRange:
        return TimeRange(start_ms=self.sermon_start_ms, end_ms=self.sermon_end_ms)


class BoundaryProposal(Model):
    boundaries: ServiceBoundaries
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str
    """Human-readable reasoning shown to the reviewer."""


class TranscriptWord(Model):
    text: str
    range: TimeRange
    probability: float | None = Field(default=None, ge=0.0, le=1.0)


class TranscriptSegment(Model):
    text: str
    range: TimeRange
    avg_logprob: float | None = None
    words: tuple[TranscriptWord, ...] = ()


class Transcript(Model):
    """Provider-neutral transcript. Times are on the source timeline."""

    language: str
    segments: tuple[TranscriptSegment, ...]
    provider: str
    model: str | None = None

    @property
    def text(self) -> str:
        return " ".join(seg.text.strip() for seg in self.segments if seg.text.strip())

    def within(self, span: TimeRange) -> "Transcript":
        """Keep the parts of the transcript overlapping `span`.

        A segment with words contributes only its words whose midpoint is inside
        the span; it is dropped when none remain. A segment without words is
        kept when its midpoint is inside the span, so partially overlapping
        segments survive only when mostly inside.
        """
        kept: list[TranscriptSegment] = []
        for seg in self.segments:
            if seg.range.end_ms <= span.start_ms or seg.range.start_ms >= span.end_ms:
                continue
            if seg.words:
                words = tuple(
                    w
                    for w in seg.words
                    if span.start_ms <= (w.range.start_ms + w.range.end_ms) / 2 < span.end_ms
                )
                if not words:
                    continue
                kept.append(
                    seg.model_copy(
                        update={
                            "text": "".join(w.text for w in words).strip(),
                            "range": TimeRange(
                                start_ms=words[0].range.start_ms,
                                end_ms=words[-1].range.end_ms,
                            ),
                            "words": words,
                        }
                    )
                )
            else:
                midpoint = (seg.range.start_ms + seg.range.end_ms) / 2
                if span.start_ms <= midpoint < span.end_ms:
                    kept.append(seg)
        return self.model_copy(update={"segments": tuple(kept)})


class FormattedTranscript(Model):
    markdown: str
    words_preserved: bool
    """True if the formatted text has exactly the source transcript's word sequence."""
    discrepancies: tuple[str, ...] = ()


class SermonMetadata(Model):
    title: str | None = None
    preacher: str | None = None
    service_date: date | None = None
    service_time: str | None = None
    """Local start time, "HH:MM"."""
    scripture: tuple[str, ...] = ()
    """Free-form references, e.g. "John 3:1-17"."""
    series: str | None = None
    occasion: str | None = None
    """E.g. the liturgical day. Optional and not tied to any tradition."""
    summary: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    """Output-specific fields passed through unchanged."""


class SourcePart(Model):
    """One media file of a recording. Interrupted recordings have several parts."""

    ref: str
    """Stable identifier within the input, e.g. a Drive file ID."""
    name: str
    size: int | None = Field(default=None, ge=0)
    checksum: str | None = None
    modified_at: datetime | None = None
    mime: str | None = None


class SourceItem(Model):
    """A completed recording discovered by an `InputSource`."""

    source_type: str
    source_ref: str
    """Stable unique identifier within `source_type`; the deduplication key."""
    parts: tuple[SourcePart, ...] = Field(min_length=1)
    """Ordered parts; the core concatenates them into one source timeline."""
    title: str | None = None
    service_date: date | None = None
    recorded_at: datetime | None = None
    url: str | None = None
    hints: dict[str, Any] = Field(default_factory=dict)
    """Free-form clues for metadata generation, e.g. companion document names."""


class MediaHeaders(Protocol):
    """Request headers for a `RemoteMedia` source, e.g. a Drive bearer token."""

    def __call__(self, *, force_refresh: bool = False) -> Mapping[str, str]:
        """Return the headers; `force_refresh` renews expired credentials."""
        ...


@dataclass(frozen=True)
class RemoteMedia:
    """An HTTP(S) source ffmpeg reads directly, instead of a downloaded file.

    `key` identifies the remote content (file id + size + checksum) so stage caches
    stay stable without a local copy.
    """

    url: str
    name: str
    key: str
    headers: MediaHeaders
    quota_retry_after: timedelta | None = None
    """Minimum retry delay when the server reports a quota or rate limit."""
    size: int | None = None
    """Total bytes, when the input knows it; the range proxy resolves it otherwise."""


MediaInput = Path | RemoteMedia
"""A part handed to the media stages: a local file or a streamable remote source."""


class ArtifactRef(Model):
    """A file the core has stored and verified."""

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    mime: str
    duration_ms: int | None = Field(default=None, ge=0)


class ExternalRef(Model):
    """Where an output put something, so it can be updated rather than duplicated."""

    target: str
    external_id: str
    url: str | None = None
    written_at: datetime | None = None
    """When Georg last wrote it successfully, if known, so the target can tell whether it was
    changed there since."""


class PublicationRequest(Model):
    publication_key: str
    """Stable idempotency key, e.g. "georg:<instance>:service:<id>:<target>"."""
    service_date: date
    metadata: SermonMetadata
    transcript_markdown: str | None = None
    files: dict[str, ArtifactRef] = Field(default_factory=dict)
    """Renditions keyed by rendition name, limited to those the output requested."""
    existing: ExternalRef | None = None
    previous: "PublicationRequest | None" = None
    """What this output last published successfully (None for a first publish), so an
    output can send only what changed."""
    skip: frozenset[str] = frozenset()
    """Output field names not to send: the reviewer kept the version edited at the target."""
    overwrite: frozenset[str] = frozenset()
    """Output field names to send even though they were edited at the target."""


class FormatContext(Model):
    language: str
    metadata: SermonMetadata


class MetadataContext(Model):
    source: SourceItem
    transcript: Transcript | None
    defaults: SermonMetadata
    """Configured defaults, e.g. the usual preacher and service time."""
    documents: dict[str, str] = Field(default_factory=dict)
    """Plain text of companion documents by name, if the generator asked for them."""


class MetadataProposal(Model):
    metadata: SermonMetadata
    origins: dict[str, str] = Field(default_factory=dict)
    """Field name -> "<module version>/<method>: <one-line reason>", for derived fields."""
