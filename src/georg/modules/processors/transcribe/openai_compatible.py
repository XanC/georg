# SPDX-License-Identifier: AGPL-3.0-only
"""`Transcriber` for OpenAI-compatible `/audio/transcriptions` endpoints.

Defaults match the lemonfox.ai service used by the original St. Paul scripts. Audio
larger than `max_upload_bytes` (or longer than `max_chunk_ms`) is cut into overlapping
mono MP3 chunks; their transcripts are shifted back onto the input's timeline.
"""

import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from georg.core.config import RenditionSpec, resolve_secret
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import TimeRange, Transcript, TranscriptSegment, TranscriptWord
from georg.llm.http import post_json
from georg.media import cut, encode_mp3, probe
from georg.modules.processors.transcribe.chunks import merge_chunks, plan_chunks

DEFAULT_BASE_URL = "https://api.lemonfox.ai/v1"
DIRECT_UPLOAD_SUFFIXES = frozenset(
    {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}
)
_MIME = {".flac": "audio/flac", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}
_SIZE_MARGIN = 0.9


class TranscriberOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: str
    """API key, normally an "env:NAME" reference."""
    base_url: str = DEFAULT_BASE_URL
    model: str | None = None
    """Sent as `model` when set (OpenAI requires e.g. "whisper-1"; lemonfox does not)."""
    language: str | None = None
    """Fallback when `transcribe` is called without a language."""
    prompt: str | None = None
    word_timestamps: bool = False
    """Request word timestamps via `timestamp_granularities[]`."""
    max_upload_bytes: int = Field(default=24_000_000, gt=0)
    max_chunk_ms: int | None = Field(default=None, gt=0)
    overlap_ms: int = Field(default=10_000, ge=0)
    chunk_bitrate_kbps: int = Field(default=48, gt=0)
    chunk_sample_rate: int = Field(default=16_000, gt=0)
    timeout_s: float = Field(default=600.0, gt=0)
    attempts: int = Field(default=3, ge=1)


def _ms(seconds: Any) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise ModuleError(f"transcription response has a non-numeric time: {seconds!r}")
    return max(0, round(seconds * 1000))


def _range(start: Any, end: Any) -> TimeRange:
    start_ms = _ms(start)
    return TimeRange(start_ms=start_ms, end_ms=max(_ms(end), start_ms + 1))


def _word(raw: Any) -> TranscriptWord:
    if not isinstance(raw, Mapping):
        raise ModuleError("transcription response has a malformed word")
    text = raw.get("word", raw.get("text", ""))
    prob = raw.get("probability")
    return TranscriptWord(
        text=str(text),
        range=_range(raw.get("start"), raw.get("end")),
        probability=min(1.0, max(0.0, float(prob))) if isinstance(prob, int | float) else None,
    )


def parse_verbose_json(data: Any) -> list[TranscriptSegment]:
    """Map a `verbose_json` transcription response to segments (times relative to the upload).

    Words are taken from each segment's `words`, or else from the top-level `words` list
    (OpenAI's layout) and assigned to the segment in which they start.
    """
    if not isinstance(data, Mapping):
        raise ModuleError("transcription response is not a JSON object")
    raw_segments = data.get("segments") or []
    raw_words = data.get("words") or []
    if not isinstance(raw_segments, list) or not isinstance(raw_words, list):
        raise ModuleError("transcription response has malformed segments or words")
    try:
        top_words = sorted((_word(w) for w in raw_words), key=lambda w: w.range.start_ms)
        segments: list[TranscriptSegment] = []
        for raw in raw_segments:
            if not isinstance(raw, Mapping):
                raise ModuleError("transcription response has a malformed segment")
            logprob = raw.get("avg_logprob")
            segments.append(
                TranscriptSegment(
                    text=str(raw.get("text", "")).strip(),
                    range=_range(raw.get("start"), raw.get("end")),
                    avg_logprob=float(logprob) if isinstance(logprob, int | float) else None,
                    words=tuple(_word(w) for w in raw.get("words") or ()),
                )
            )
    except ValidationError as exc:
        raise ModuleError(f"transcription response is invalid: {exc}") from exc
    segments.sort(key=lambda s: s.range.start_ms)
    if not segments and isinstance(data.get("text"), str) and data["text"].strip():
        end = _ms(data.get("duration") or 0)
        segments = [TranscriptSegment(text=data["text"].strip(), range=_range(0, end / 1000))]
    if top_words and segments and not any(s.words for s in segments):
        segments = _assign_words(segments, top_words)
    return segments


def _assign_words(
    segments: list[TranscriptSegment], words: list[TranscriptWord]
) -> list[TranscriptSegment]:
    buckets: list[list[TranscriptWord]] = [[] for _ in segments]
    i = 0
    for word in words:
        while i + 1 < len(segments) and segments[i + 1].range.start_ms <= word.range.start_ms:
            i += 1
        buckets[i].append(word)
    return [
        s.model_copy(update={"words": tuple(b)}) for s, b in zip(segments, buckets, strict=True)
    ]


class OpenAICompatibleTranscriber:
    """Module type `openai_compatible`."""

    version = "openai_compatible-1"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            self.options = TranscriberOptions.model_validate(dict(options))
        except ValidationError as exc:
            raise ConfigError(f"transcriber options: {exc}") from exc
        opts = self.options
        self._chunk_ms = int(opts.max_upload_bytes * _SIZE_MARGIN / (opts.chunk_bitrate_kbps / 8))
        if opts.max_chunk_ms is not None:
            self._chunk_ms = min(self._chunk_ms, opts.max_chunk_ms)
        if self._chunk_ms <= 2 * opts.overlap_ms:
            raise ConfigError("transcriber chunk length must exceed twice overlap_ms")
        self._url = opts.base_url.rstrip("/") + "/audio/transcriptions"
        self._provider = urlparse(opts.base_url).hostname or "openai_compatible"
        self._headers = {
            "Authorization": f"Bearer {resolve_secret(opts.api_key)}",
            "Accept": "application/json",
        }
        self._client = http_client or httpx.Client(timeout=opts.timeout_s)
        self._sleep = sleep

    def transcribe(self, audio: Path, language: str | None) -> Transcript:
        lang = language or self.options.language
        opts = self.options
        direct = (
            audio.suffix.lower() in DIRECT_UPLOAD_SUFFIXES
            and audio.stat().st_size <= opts.max_upload_bytes
            and opts.max_chunk_ms is None
        )
        reported_language: str | None = None
        if direct:
            data = self._upload(audio, lang)
            chunks = [(TimeRange(start_ms=0, end_ms=1), parse_verbose_json(data))]
            reported_language = data.get("language")
        else:
            chunks = []
            duration = probe(audio).duration_ms
            spans = plan_chunks(duration, self._chunk_ms, opts.overlap_ms)
            spec = RenditionSpec(
                name="transcribe_chunk",
                span="service",
                channels=1,
                sample_rate=opts.chunk_sample_rate,
                bitrate_kbps=opts.chunk_bitrate_kbps,
            )
            with tempfile.TemporaryDirectory(prefix="georg-transcribe-") as tmp:
                for i, span in enumerate(spans):
                    src = audio
                    if len(spans) > 1:
                        src = cut(audio, span, Path(tmp) / f"chunk{i:03d}.flac")
                    mp3 = encode_mp3(src, Path(tmp) / f"chunk{i:03d}.mp3", spec)
                    if src != audio:
                        src.unlink()
                    if mp3.stat().st_size > opts.max_upload_bytes:
                        raise ModuleError(
                            f"chunk {i} is {mp3.stat().st_size} bytes, over max_upload_bytes"
                        )
                    data = self._upload(mp3, lang)
                    mp3.unlink()
                    reported_language = reported_language or data.get("language")
                    chunks.append((span, parse_verbose_json(data)))
        segments = merge_chunks(chunks)
        return Transcript(
            language=lang or (reported_language if isinstance(reported_language, str) else "und"),
            segments=tuple(segments),
            provider=self._provider,
            model=opts.model,
        )

    def _upload(self, path: Path, language: str | None) -> Any:
        opts = self.options
        fields: dict[str, str | list[str]] = {"response_format": "verbose_json"}
        if opts.model:
            fields["model"] = opts.model
        if language:
            fields["language"] = language
        if opts.prompt:
            fields["prompt"] = opts.prompt
        if opts.word_timestamps:
            fields["timestamp_granularities[]"] = ["segment", "word"]
        mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
        content = path.read_bytes()
        data = post_json(
            self._client,
            self._url,
            provider=self._provider,
            attempts=opts.attempts,
            sleep=self._sleep,
            headers=self._headers,
            data=fields,
            files={"file": (path.name, content, mime)},
        )
        if not isinstance(data, Mapping):
            raise ModuleError(f"{self._provider}: transcription response is not a JSON object")
        return data
