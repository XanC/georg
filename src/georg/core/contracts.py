# SPDX-License-Identifier: AGPL-3.0-only
"""Interfaces implemented by input, processor, and output modules.

Modules receive their configuration when constructed and must not touch the
database, job queue, artifact store, or web layer. They raise `ModuleError`
(with `retryable=True` for transient failures) rather than returning error values.
Each processor exposes `version`; changing behavior must change the version so
cached results are recomputed.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from georg.core.types import (
    AudioSegment,
    BoundaryProposal,
    ExternalRef,
    FormatContext,
    FormattedTranscript,
    MetadataContext,
    PublicationRequest,
    SermonMetadata,
    SourceItem,
    Transcript,
)


@runtime_checkable
class InputSource(Protocol):
    name: str

    def discover(self, since: datetime | None) -> Iterable[SourceItem]:
        """Yield recordings that are complete (no longer uploading). May repeat items."""
        ...

    def fetch(self, item: SourceItem, dest_dir: Path) -> list[Path]:
        """Download every part into `dest_dir`; return local paths in part order.

        Raise `SourceNotReadyError` if a part changed since discovery.
        """
        ...


@runtime_checkable
class AudioClassifier(Protocol):
    version: str

    def classify(self, audio: Path) -> list[AudioSegment]:
        """Label the whole file; segments are sorted and non-overlapping."""
        ...


@runtime_checkable
class BoundaryDetector(Protocol):
    version: str

    def detect(
        self,
        segments: Sequence[AudioSegment],
        duration_ms: int,
        transcript: Transcript | None,
    ) -> BoundaryProposal:
        """Propose service and sermon boundaries. Pure function; no I/O."""
        ...


@runtime_checkable
class Transcriber(Protocol):
    version: str

    def transcribe(self, audio: Path, language: str | None) -> Transcript:
        """Transcribe the file; times are relative to the start of `audio`."""
        ...


@runtime_checkable
class TranscriptFormatter(Protocol):
    version: str

    def format(self, transcript: Transcript, ctx: FormatContext) -> FormattedTranscript:
        """Add paragraphs and punctuation without changing words."""
        ...


@runtime_checkable
class MetadataGenerator(Protocol):
    version: str

    def generate(self, ctx: MetadataContext) -> SermonMetadata:
        """Propose metadata; unknown fields stay None for the reviewer."""
        ...


@runtime_checkable
class OutputTarget(Protocol):
    name: str
    renditions: frozenset[str]
    """Rendition names this output needs in `PublicationRequest.files`."""
    wants_transcript: bool

    def find_existing(self, publication_key: str) -> ExternalRef | None:
        """Return the object previously published under this key, if any."""
        ...

    def publish(self, request: PublicationRequest) -> ExternalRef:
        """Create, or update `request.existing`. Must be safe to retry."""
        ...
