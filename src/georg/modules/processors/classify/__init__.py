# SPDX-License-Identifier: AGPL-3.0-only
"""Audio classifiers: label a recording as speech, music, noise, or silence."""

from georg.modules.processors.classify.ina import InaOptions, InaSpeechSegmenterClassifier
from georg.modules.processors.classify.ina_yamnet import (
    InaYamnetClassifier,
    InaYamnetOptions,
)
from georg.modules.processors.classify.segments import (
    normalize_segments,
    segments_from_json,
    segments_to_json,
)
from georg.modules.processors.classify.yamnet import YamnetOptions

__all__ = [
    "InaOptions",
    "InaSpeechSegmenterClassifier",
    "InaYamnetClassifier",
    "InaYamnetOptions",
    "YamnetOptions",
    "normalize_segments",
    "segments_from_json",
    "segments_to_json",
]
