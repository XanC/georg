# SPDX-License-Identifier: AGPL-3.0-only
"""Audio classifier backed by inaSpeechSegmenter (MIT; TensorFlow CNNs).

inaSpeechSegmenter and TensorFlow are an optional extra (`georg[classify]`) and are
imported only when the first file is classified. Its model weights are downloaded
from the project's GitHub releases on first use and cached under `$KERAS_HOME`
(default `~/.keras`).
"""

from collections.abc import Callable, Iterable
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from georg.core.errors import ConfigError, ModuleError
from georg.core.types import AudioSegment, SegmentLabel
from georg.media import probe
from georg.media.ffmpeg import FFMPEG
from georg.modules.processors.classify.segments import normalize_segments

TYPE_NAME = "ina_speech_segmenter"
_REVISION = 1

LABELS: dict[str, SegmentLabel] = {
    "male": "speech",
    "female": "speech",
    "speech": "speech",
    "music": "music",
    "noise": "noise",
    "noEnergy": "silence",
}


class Segmenter(Protocol):
    """The callable interface of `inaSpeechSegmenter.Segmenter`."""

    def __call__(
        self, medianame: str, start_sec: float | None = None, stop_sec: float | None = None
    ) -> Iterable[tuple[str, float, float]]: ...


class InaOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vad_engine: Literal["smn", "sm"] = "smn"
    """"smn" distinguishes speech/music/noise; "sm" only speech/music."""
    energy_ratio: float = Field(default=0.03, gt=0.0, lt=1.0)
    """Frames below this fraction of the file's mean energy are labeled silence."""
    batch_size: int = Field(default=256, ge=1)
    chunk_s: int | None = Field(default=600, ge=60)
    """Classify in windows of this many seconds to bound memory; None for the whole file."""


def _library_version() -> str:
    try:
        return metadata.version("inaSpeechSegmenter")
    except metadata.PackageNotFoundError:
        return "missing"


def _load_segmenter(options: InaOptions) -> Segmenter:
    try:
        from inaSpeechSegmenter import Segmenter as InaSegmenter
    except ImportError as exc:
        raise ModuleError(
            "inaSpeechSegmenter is not installed; install the 'classify' extra "
            "(uv sync --extra classify)"
        ) from exc
    try:
        segmenter: Segmenter = InaSegmenter(
            vad_engine=options.vad_engine,
            detect_gender=False,
            ffmpeg=FFMPEG,
            batch_size=options.batch_size,
            energy_ratio=options.energy_ratio,
        )
    except OSError as exc:
        raise ModuleError(
            f"could not load inaSpeechSegmenter models: {exc}", retryable=True
        ) from exc
    except Exception as exc:
        raise ModuleError(f"could not initialize inaSpeechSegmenter: {exc}") from exc
    return segmenter


class InaSpeechSegmenterClassifier:
    """`AudioClassifier` that labels a whole service recording in one pass."""

    type_name = TYPE_NAME

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        segmenter_factory: Callable[[InaOptions], Segmenter] = _load_segmenter,
    ) -> None:
        try:
            self.options = InaOptions.model_validate(options or {})
        except ValidationError as exc:
            raise ConfigError(f"{TYPE_NAME}: {exc}") from exc
        self._factory = segmenter_factory
        self._segmenter: Segmenter | None = None
        o = self.options
        self.version = (
            f"{TYPE_NAME}-r{_REVISION}-ina{_library_version()}"
            f"-{o.vad_engine}-e{o.energy_ratio}-c{o.chunk_s or 0}"
        )

    def classify(self, audio: Path) -> list[AudioSegment]:
        duration_ms = probe(audio).duration_ms
        if self._segmenter is None:
            self._segmenter = self._factory(self.options)
        raw: list[tuple[SegmentLabel, int, int]] = []
        for start_s, stop_s in self._windows(duration_ms):
            try:
                found = list(self._segmenter(str(audio), start_s, stop_s))
            except Exception as exc:
                raise ModuleError(f"inaSpeechSegmenter failed on {audio}: {exc}") from exc
            raw.extend(_map(found))
        return normalize_segments(raw, duration_ms)

    def _windows(self, duration_ms: int) -> list[tuple[float | None, float | None]]:
        chunk = self.options.chunk_s
        if chunk is None or duration_ms <= chunk * 1000:
            return [(None, None)]
        starts = range(0, duration_ms, chunk * 1000)
        return [(s / 1000, min(s + chunk * 1000, duration_ms) / 1000) for s in starts]


def _map(found: Iterable[tuple[str, float, float]]) -> list[tuple[SegmentLabel, int, int]]:
    out = []
    for label, start, end in found:
        try:
            mapped = LABELS[label]
        except KeyError:
            raise ModuleError(f"inaSpeechSegmenter returned unknown label {label!r}") from None
        out.append((mapped, round(start * 1000), round(end * 1000)))
    return out
