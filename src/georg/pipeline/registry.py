# SPDX-License-Identifier: AGPL-3.0-only
"""Maps configured module `type` names to implementations, importing them lazily."""

import importlib
from dataclasses import dataclass
from typing import Any, Literal

from georg.core.config import ModuleConfig, ProcessorsConfig
from georg.core.contracts import (
    AudioClassifier,
    BoundaryDetector,
    MetadataGenerator,
    Transcriber,
    TranscriptFormatter,
)
from georg.core.errors import ConfigError

Role = Literal["classifier", "detector", "transcriber", "formatter", "metadata"]

_P = "georg.modules.processors"
PROCESSOR_TYPES: dict[Role, dict[str, str]] = {
    "classifier": {"ina_speech_segmenter": f"{_P}.classify:InaSpeechSegmenterClassifier"},
    "detector": {"heuristic": f"{_P}.detect:HeuristicBoundaryDetector"},
    "transcriber": {
        "faster_whisper": f"{_P}.transcribe:FasterWhisperTranscriber",
        "openai_compatible": f"{_P}.transcribe:OpenAICompatibleTranscriber",
    },
    "formatter": {"openai_chat": f"{_P}.format:OpenAIChatFormatter"},
    "metadata": {"rules": f"{_P}.metadata:RulesMetadataGenerator"},
}


def build_processor(role: Role, config: ModuleConfig) -> Any:
    """Construct the module registered for `config.type` in `role`."""
    target = PROCESSOR_TYPES[role].get(config.type)
    if target is None:
        known = ", ".join(sorted(PROCESSOR_TYPES[role]))
        raise ConfigError(f"unknown {role} type {config.type!r} (known: {known})")
    module_name, _, attr = target.partition(":")
    cls = getattr(importlib.import_module(module_name), attr)
    factory = getattr(cls, "from_options", cls)
    return factory(dict(config.options))


@dataclass(frozen=True)
class Processors:
    classifier: AudioClassifier
    detector: BoundaryDetector
    transcriber: Transcriber
    formatter: TranscriptFormatter | None
    metadata: MetadataGenerator


def build_processors(config: ProcessorsConfig, *, formatter: bool = True) -> Processors:
    """Build every processor; skip the formatter when `formatter` is false."""
    return Processors(
        classifier=build_processor("classifier", config.classifier),
        detector=build_processor("detector", config.detector),
        transcriber=build_processor("transcriber", config.transcriber),
        formatter=build_processor("formatter", config.formatter) if formatter else None,
        metadata=build_processor("metadata", config.metadata),
    )
