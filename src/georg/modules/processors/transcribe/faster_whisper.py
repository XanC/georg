# SPDX-License-Identifier: AGPL-3.0-only
"""`Transcriber` that runs OpenAI's Whisper locally with faster-whisper (CTranslate2).

faster-whisper is an optional extra (`georg[whisper]`) imported on first use. Model
weights are downloaded from Hugging Face on first use into `download_root` (default:
the Hugging Face cache); set `local_files_only` on hosts without network access.

Audio is decoded with Georg's FFmpeg rather than faster-whisper's bundled PyAV decoder,
so any input FFmpeg reads works and PyAV version changes cannot break decoding.
"""

import subprocess
from collections.abc import Callable, Iterable
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from georg.core.errors import ConfigError, ModuleError
from georg.core.types import TimeRange, Transcript, TranscriptSegment, TranscriptWord
from georg.media.ffmpeg import FFMPEG

TYPE_NAME = "faster_whisper"
_REVISION = 1
SAMPLE_RATE = 16_000


class WhisperWordLike(Protocol):
    start: float
    end: float
    word: str
    probability: float


class WhisperSegmentLike(Protocol):
    start: float
    end: float
    text: str
    avg_logprob: float
    words: list[Any] | None


class WhisperInfoLike(Protocol):
    language: str


class WhisperModelLike(Protocol):
    """The subset of `faster_whisper.WhisperModel` that Georg uses."""

    def transcribe(
        self, audio: Any, **kwargs: Any
    ) -> tuple[Iterable[WhisperSegmentLike], WhisperInfoLike]: ...


class FasterWhisperOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "large-v3-turbo"
    """A faster-whisper model name (e.g. "small", "medium", "large-v3") or a local path."""
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    compute_type: str = "int8"
    """CTranslate2 quantization, e.g. "int8" (CPU), "float16" (GPU), "default"."""
    cpu_threads: int = Field(default=0, ge=0)
    """0 lets CTranslate2 choose."""
    language: str | None = None
    """Fallback when `transcribe` is called without a language; None auto-detects."""
    prompt: str | None = None
    """Passed as Whisper's `initial_prompt` (names, vocabulary)."""
    beam_size: int = Field(default=5, ge=1)
    vad_filter: bool = False
    word_timestamps: bool = False
    download_root: Path | None = None
    local_files_only: bool = False


def _library_version() -> str:
    try:
        return metadata.version("faster-whisper")
    except metadata.PackageNotFoundError:
        return "missing"


def _load_model(options: FasterWhisperOptions) -> WhisperModelLike:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ModuleError(
            "faster-whisper is not installed; install the 'whisper' extra (uv sync --extra whisper)"
        ) from exc
    try:
        model: WhisperModelLike = WhisperModel(
            options.model,
            device=options.device,
            compute_type=options.compute_type,
            cpu_threads=options.cpu_threads,
            download_root=str(options.download_root) if options.download_root else None,
            local_files_only=options.local_files_only,
        )
    except OSError as exc:
        raise ModuleError(
            f"could not load Whisper model {options.model!r}: {exc}", retryable=True
        ) from exc
    except Exception as exc:
        raise ModuleError(f"could not initialize Whisper model {options.model!r}: {exc}") from exc
    return model


def decode_pcm(audio: Path) -> Any:
    """Decode `audio` to the 16 kHz mono float32 numpy array Whisper expects."""
    try:
        import numpy as np
    except ImportError as exc:
        raise ModuleError("numpy is not installed; install the 'whisper' extra") from exc
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(audio),
        "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as exc:
        raise ModuleError(f"could not run ffmpeg: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise ModuleError(f"ffmpeg could not decode {audio}: {detail}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _range(start: float, end: float) -> TimeRange:
    start_ms = max(0, round(start * 1000))
    return TimeRange(start_ms=start_ms, end_ms=max(round(end * 1000), start_ms + 1))


def _segment(raw: WhisperSegmentLike) -> TranscriptSegment:
    words = tuple(
        TranscriptWord(
            text=w.word,
            range=_range(w.start, w.end),
            probability=min(1.0, max(0.0, float(w.probability))),
        )
        for w in raw.words or ()
    )
    return TranscriptSegment(
        text=raw.text.strip(),
        range=_range(raw.start, raw.end),
        avg_logprob=float(raw.avg_logprob),
        words=words,
    )


class FasterWhisperTranscriber:
    """`Transcriber` running Whisper on this machine; no network access after model download."""

    type_name = TYPE_NAME

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        model_factory: Callable[[FasterWhisperOptions], WhisperModelLike] = _load_model,
        audio_decoder: Callable[[Path], Any] = decode_pcm,
    ) -> None:
        try:
            self.options = FasterWhisperOptions.model_validate(options or {})
        except ValidationError as exc:
            raise ConfigError(f"{TYPE_NAME}: {exc}") from exc
        self._factory = model_factory
        self._decode = audio_decoder
        self._model: WhisperModelLike | None = None
        o = self.options
        self.version = (
            f"{TYPE_NAME}-r{_REVISION}-fw{_library_version()}-{Path(o.model).name}"
            f"-{o.compute_type}-b{o.beam_size}-vad{int(o.vad_filter)}-w{int(o.word_timestamps)}"
        )

    def transcribe(self, audio: Path, language: str | None) -> Transcript:
        o = self.options
        samples = self._decode(audio)
        if self._model is None:
            self._model = self._factory(o)
        try:
            raw_segments, info = self._model.transcribe(
                samples,
                language=language or o.language,
                initial_prompt=o.prompt,
                beam_size=o.beam_size,
                vad_filter=o.vad_filter,
                word_timestamps=o.word_timestamps,
            )
            segments = [_segment(s) for s in raw_segments]
        except (ValidationError, ModuleError):
            raise
        except Exception as exc:
            raise ModuleError(f"faster-whisper failed on {audio}: {exc}") from exc
        segments.sort(key=lambda s: s.range.start_ms)
        return Transcript(
            language=language or o.language or info.language or "und",
            segments=tuple(segments),
            provider=TYPE_NAME,
            model=o.model,
        )
