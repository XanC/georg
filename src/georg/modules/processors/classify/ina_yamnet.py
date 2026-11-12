# SPDX-License-Identifier: AGPL-3.0-only
"""inaSpeechSegmenter classifier with YAMNet-rescued music.

Runs inaSpeechSegmenter as usual, then rechecks each `noise` segment with YAMNet:
if at least `music_fraction` of the YAMNet frames inside it are labeled music, the
segment becomes `music`. ina calls organ postludes `noise` while YAMNet calls them
music, which is exactly the rescue wanted for service ends; ina's other labels
(including its `noise` for pre-service congregation talk, which YAMNet hears as
speech) are left untouched, and noise is never created.
"""

import gc
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from georg.core.errors import ConfigError
from georg.core.types import AudioSegment, SegmentLabel
from georg.media import probe
from georg.modules.processors.classify import yamnet
from georg.modules.processors.classify.ina import (
    InaOptions,
    InaSpeechSegmenterClassifier,
    Segmenter,
    _load_segmenter,
)
from georg.modules.processors.classify.segments import normalize_segments
from georg.modules.processors.classify.yamnet import YamnetModel, YamnetOptions

log = logging.getLogger(__name__)

TYPE_NAME = "ina_yamnet"
_REVISION = 1


class InaYamnetOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    music_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    """Relabel an ina `noise` segment `music` when this fraction of the YAMNet
    frames inside it are music."""
    ina: InaOptions = Field(default_factory=InaOptions)
    """Options passed through to the inaSpeechSegmenter classifier."""
    yamnet: YamnetOptions = Field(default_factory=YamnetOptions)
    """Options passed through to the YAMNet frame scorer."""


class InaYamnetClassifier:
    """`AudioClassifier` (type "ina_yamnet"): ina plus YAMNet noise-to-music rescue."""

    type_name = TYPE_NAME

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        segmenter_factory: Callable[[InaOptions], Segmenter] = _load_segmenter,
        model_factory: Callable[[YamnetOptions], YamnetModel] = yamnet.load_model,
    ) -> None:
        try:
            self.options = InaYamnetOptions.model_validate(options or {})
        except ValidationError as exc:
            raise ConfigError(f"{TYPE_NAME}: {exc}") from exc
        self._factory = model_factory
        self._model: YamnetModel | None = None
        self._ina = InaSpeechSegmenterClassifier(
            self.options.ina.model_dump(), segmenter_factory=segmenter_factory
        )
        o = self.options
        y = o.yamnet
        self.version = (
            f"{TYPE_NAME}-r{_REVISION}-f{o.music_fraction}-{self._ina.version}"
            f"-yu{y.url_key()}-c{y.chunk_s}-o{y.overlap_s}"
            f"-n{y.noise_score}-m{y.majority_frames}"
        )

    def classify(self, audio: Path) -> list[AudioSegment]:
        duration_ms = probe(audio).duration_ms
        segments = self._ina.classify(audio)
        if not any(s.label == "noise" for s in segments):
            return segments
        if self._model is None:
            self._model = self._factory(self.options.yamnet)
        frames = yamnet.frame_labels(audio, duration_ms, self._model, self.options.yamnet)
        raw: list[tuple[SegmentLabel, int, int]] = []
        for s in segments:
            if s.label != "noise":
                raw.append((s.label, s.range.start_ms, s.range.end_ms))
                continue
            lo = s.range.start_ms // yamnet.HOP_MS
            hi = -(-s.range.end_ms // yamnet.HOP_MS)
            inside = frames[lo:hi]
            music = sum(1 for f in inside if f == "music")
            fraction = music / len(inside) if inside else 0.0
            label: SegmentLabel = (
                "music" if inside and fraction >= self.options.music_fraction else s.label
            )
            if label == "music":
                log.info(
                    "ina noise %d-%d relabeled music (%.0f%% of %d YAMNet frames)",
                    s.range.start_ms,
                    s.range.end_ms,
                    100 * music / len(inside),
                    len(inside),
                )
            raw.append((label, s.range.start_ms, s.range.end_ms))
        return normalize_segments(raw, duration_ms)

    def release(self) -> None:
        """Free both the ina segmenter and the YAMNet model."""
        self._ina.release()
        if self._model is None:
            return
        self._model = None
        yamnet.release_model()
        gc.collect()
