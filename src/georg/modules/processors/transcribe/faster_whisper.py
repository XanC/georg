# SPDX-License-Identifier: AGPL-3.0-only
"""`Transcriber` that runs OpenAI's Whisper locally with faster-whisper (CTranslate2).

faster-whisper is an optional extra (`georg[whisper]`) imported on first use. Model
weights are downloaded from Hugging Face on first use into `download_root` (default:
the Hugging Face cache); set `local_files_only` on hosts without network access.

Audio is decoded with Georg's FFmpeg rather than faster-whisper's bundled PyAV decoder,
so any input FFmpeg reads works and PyAV version changes cannot break decoding.
"""

import hashlib
import itertools
import json
import logging
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
_REVISION = 3
SAMPLE_RATE = 16_000

_logger = logging.getLogger(__name__)

# Texts Whisper hallucinates over silence, music and noise. Compared after
# normalization, so bracketed/all-caps stage directions ("[Music]", "CHOIR SINGS")
# match too.
_HALLUCINATION_TEXTS = frozenset(
    {"thank you", "thanks for watching", "you", "choir sings", "music", "applause"}
)
_REPEAT_RUN_MIN = 3
_REPEAT_RUN_MIN_SECONDS = 20.0
_HALLUCINATION_MIN_SECONDS = 20.0
_HALLUCINATION_MAX_WORDS = 3


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
    vad_filter: bool = True
    """Silero VAD before transcription. On by default: without it long recordings
    with music/noise can loop the same hallucinated segment for the whole file."""
    vad_parameters: dict[str, Any] | None = None
    """Passed through as faster-whisper's `vad_parameters` (e.g. threshold,
    min_silence_duration_ms) when set."""
    condition_on_previous_text: bool = False
    """Off by default: conditioning on previous text lets one hallucination drag
    the following windows into the same loop."""
    hallucination_silence_threshold: float | None = None
    """Seconds of silence that mark a segment as a hallucination candidate.
    Only passed when set; faster-whisper requires `word_timestamps=True` for it."""
    hotwords: str | None = None
    """Passed through as faster-whisper's `hotwords` prompt."""
    word_timestamps: bool = True
    """On by default: word timings let Georg split segments that stretch across
    long non-speech regions (a VAD artefact) and trim them at sermon bounds."""
    split_gap_seconds: float = Field(default=2.0, ge=0.0)
    """Segments are split wherever consecutive words are this far apart or more."""
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


def _normalized(text: str) -> str:
    return " ".join("".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace()).split())


def drop_repeated_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Remove hallucinated repetition from raw Whisper output.

    Two filters: runs of >=3 consecutive segments with identical normalized text
    spanning >=20 s (the classic Whisper "Thank you." loop over music/noise), and
    lone >=20 s segments of <=3 words matching a known-hallucination text.
    """

    def _drop(group: list[TranscriptSegment], why: str) -> None:
        _logger.info(
            "faster_whisper: dropped %d segment(s) %d-%d ms (%s; %r)",
            len(group),
            group[0].range.start_ms,
            group[-1].range.end_ms,
            why,
            group[0].text,
        )

    kept: list[TranscriptSegment] = []
    run: list[TranscriptSegment] = []

    def flush() -> None:
        if not run:
            return
        span = run[-1].range.end_ms - run[0].range.start_ms
        if len(run) >= _REPEAT_RUN_MIN and span >= _REPEAT_RUN_MIN_SECONDS * 1000:
            _drop(run, f"repeated {len(run)} times over {span / 1000:.1f} s")
        else:
            kept.extend(run)

    for seg in segments:
        if run and _normalized(seg.text) == _normalized(run[-1].text):
            run.append(seg)
        else:
            flush()
            run = [seg]
    flush()

    result = []
    for seg in kept:
        duration_s = (seg.range.end_ms - seg.range.start_ms) / 1000
        normalized = _normalized(seg.text)
        if (
            duration_s >= _HALLUCINATION_MIN_SECONDS
            and len(normalized.split()) <= _HALLUCINATION_MAX_WORDS
            and normalized in _HALLUCINATION_TEXTS
        ):
            _drop([seg], "known hallucination")
        else:
            result.append(seg)
    return result


def split_at_word_gaps(
    segments: list[TranscriptSegment], gap_seconds: float
) -> list[TranscriptSegment]:
    """Split segments whose words have a gap of >= `gap_seconds` between them.

    With `vad_filter`, faster-whisper can return one segment whose timestamps
    stretch across a long non-speech region. Splitting at word gaps gives each
    piece a truthful range, which `Transcript.within` and the boundary detector
    rely on. Segments without words pass through unchanged, and
    `split_gap_seconds = 0` disables splitting.
    """
    gap_ms = round(gap_seconds * 1000)
    if gap_ms <= 0:
        return list(segments)
    out: list[TranscriptSegment] = []
    for seg in segments:
        words = list(seg.words)
        cuts = [
            i
            for i in range(1, len(words))
            if words[i].range.start_ms - words[i - 1].range.end_ms >= gap_ms
        ]
        if not cuts:
            out.append(seg)
            continue
        bounds = [0, *cuts, len(words)]
        for lo, hi in itertools.pairwise(bounds):
            piece = words[lo:hi]
            out.append(
                seg.model_copy(
                    update={
                        "text": "".join(w.text for w in piece).strip(),
                        "range": TimeRange(
                            start_ms=piece[0].range.start_ms,
                            end_ms=piece[-1].range.end_ms,
                        ),
                        "words": tuple(piece),
                    }
                )
            )
    return out


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
        extra = {
            k: v
            for k, v in {
                "vad_parameters": o.vad_parameters,
                "hallucination_silence_threshold": o.hallucination_silence_threshold,
                "hotwords": o.hotwords,
                "prompt": o.prompt,
            }.items()
            if v is not None
        }
        self.version = (
            f"{TYPE_NAME}-r{_REVISION}-fw{_library_version()}-{Path(o.model).name}"
            f"-{o.compute_type}-b{o.beam_size}-vad{int(o.vad_filter)}"
            f"-c{int(o.condition_on_previous_text)}-w{int(o.word_timestamps)}"
            f"-gap{o.split_gap_seconds:g}"
        )
        if extra:
            digest = hashlib.sha256(
                json.dumps(extra, sort_keys=True, default=str).encode()
            ).hexdigest()[:8]
            self.version += f"-x{digest}"

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
                condition_on_previous_text=o.condition_on_previous_text,
                **({"vad_parameters": o.vad_parameters} if o.vad_parameters is not None else {}),
                **(
                    {"hallucination_silence_threshold": o.hallucination_silence_threshold}
                    if o.hallucination_silence_threshold is not None
                    else {}
                ),
                **({"hotwords": o.hotwords} if o.hotwords is not None else {}),
            )
            segments = drop_repeated_segments(
                split_at_word_gaps([_segment(s) for s in raw_segments], o.split_gap_seconds)
            )
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
