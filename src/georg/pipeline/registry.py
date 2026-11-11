# SPDX-License-Identifier: AGPL-3.0-only
"""Maps configured module `type` names to implementations, importing them lazily."""

import ctypes
import ctypes.util
import importlib
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Literal

from georg.core.config import (
    CalendarConfig,
    ModuleConfig,
    NamedModuleConfig,
    OutputConfig,
    ProcessorsConfig,
)
from georg.core.contracts import (
    AudioClassifier,
    BoundaryDetector,
    InputSource,
    MetadataGenerator,
    OutputTarget,
    Transcriber,
    TranscriptFormatter,
)
from georg.core.errors import ConfigError

log = logging.getLogger(__name__)

Role = Literal["classifier", "detector", "transcriber", "formatter", "metadata"]

_P = "georg.modules.processors"
PROCESSOR_TYPES: dict[Role, dict[str, str]] = {
    "classifier": {"ina_speech_segmenter": f"{_P}.classify:InaSpeechSegmenterClassifier"},
    "detector": {"heuristic": f"{_P}.detect:HeuristicBoundaryDetector"},
    "transcriber": {
        "faster_whisper": f"{_P}.transcribe:FasterWhisperTranscriber",
        "openai_compatible": f"{_P}.transcribe:OpenAICompatibleTranscriber",
    },
    "formatter": {
        "llama_cpp": f"{_P}.format:LlamaCppFormatter",
        "openai_chat": f"{_P}.format:OpenAIChatFormatter",
    },
    "metadata": {"rules": f"{_P}.metadata:RulesMetadataGenerator"},
}

INPUT_TYPES: dict[str, str] = {
    "google_drive": "georg.modules.inputs.google_drive:GoogleDriveInput",
    "local_folder": "georg.modules.inputs.local_folder:LocalFolderInput",
}

OUTPUT_TYPES: dict[str, str] = {
    "google_drive_upload": "georg.modules.outputs.google_drive_upload:GoogleDriveUploadOutput",
    "sftp": "georg.modules.outputs.sftp:SftpOutput",
    "wordpress": "georg.modules.outputs.wordpress:WordPressOutput",
}
"""Output classes are constructed with their whole `OutputConfig`."""


def build_output(config: OutputConfig, *, timezone: str = "UTC") -> OutputTarget:
    """Construct the output; pass `timezone` to classes that take `default_timezone`."""
    target = OUTPUT_TYPES.get(config.type)
    if target is None:
        known = ", ".join(sorted(OUTPUT_TYPES))
        raise ConfigError(f"unknown output type {config.type!r} (known: {known})")
    module_name, _, attr = target.partition(":")
    cls = getattr(importlib.import_module(module_name), attr)
    kwargs = {}
    if "default_timezone" in inspect.signature(cls).parameters:
        kwargs["default_timezone"] = timezone
    output: OutputTarget = cls(config, **kwargs)
    return output


def _construct(target: str, options: dict[str, Any]) -> Any:
    module_name, _, attr = target.partition(":")
    cls = getattr(importlib.import_module(module_name), attr)
    factory = getattr(cls, "from_options", cls)
    return factory(dict(options))


def build_input(config: NamedModuleConfig) -> InputSource:
    """Construct the input registered for `config.type`."""
    target = INPUT_TYPES.get(config.type)
    if target is None:
        known = ", ".join(sorted(INPUT_TYPES))
        raise ConfigError(f"unknown input type {config.type!r} (known: {known})")
    source: InputSource = _construct(target, config.options)
    return source


def build_processor(role: Role, config: ModuleConfig) -> Any:
    """Construct the module registered for `config.type` in `role`."""
    target = PROCESSOR_TYPES[role].get(config.type)
    if target is None:
        known = ", ".join(sorted(PROCESSOR_TYPES[role]))
        raise ConfigError(f"unknown {role} type {config.type!r} (known: {known})")
    return _construct(target, config.options)


@dataclass(frozen=True)
class Processors:
    classifier: AudioClassifier
    detector: BoundaryDetector
    transcriber: Transcriber
    formatter: TranscriptFormatter | None
    metadata: MetadataGenerator
    sermon_transcriber: Transcriber | None = None
    sermon_transcribe_pad_ms: int = 2_000

    def release(self) -> None:
        """Free every module's loaded model; each reloads on its next use."""
        release_models(
            self.classifier,
            self.detector,
            self.transcriber,
            self.sermon_transcriber,
            self.metadata,
            self.formatter,
        )


def release_models(*modules: object) -> None:
    """Call each module's optional `release()` (once per module), so a loaded model's
    memory is freed before the next model-heavy step. Failures are logged, not raised."""
    seen: set[int] = set()
    for module in modules:
        if module is None or id(module) in seen:
            continue
        seen.add(id(module))
        release = getattr(module, "release", None)
        if not callable(release):
            continue
        try:
            release()
        except Exception:
            log.warning("could not release %s", type(module).__name__, exc_info=True)
    if seen:
        _trim_heap()


def _trim_heap() -> None:
    """Return freed heap pages to the OS (glibc keeps them otherwise; no-op elsewhere)."""
    name = ctypes.util.find_library("c")
    try:
        libc = ctypes.CDLL(name)
        libc.malloc_trim(0)
    except (OSError, AttributeError, TypeError):
        pass


def build_processors(
    config: ProcessorsConfig, *, formatter: bool = True, calendar: CalendarConfig | None = None
) -> Processors:
    """Build every processor; skip the formatter when `formatter` is false.

    A metadata generator whose constructor takes `calendar` gets the configured day names.
    """
    return Processors(
        classifier=build_processor("classifier", config.classifier),
        detector=build_processor("detector", config.detector),
        transcriber=build_processor("transcriber", config.transcriber),
        formatter=build_processor("formatter", config.formatter) if formatter else None,
        metadata=_build_metadata(config.metadata, calendar or CalendarConfig()),
        sermon_transcriber=(
            build_processor("transcriber", config.sermon_transcriber)
            if config.sermon_transcriber is not None
            else None
        ),
        sermon_transcribe_pad_ms=config.sermon_transcribe_pad_ms,
    )


def _build_metadata(config: ModuleConfig, calendar: CalendarConfig) -> Any:
    target = PROCESSOR_TYPES["metadata"].get(config.type)
    if target is None:
        return build_processor("metadata", config)
    module_name, _, attr = target.partition(":")
    cls = getattr(importlib.import_module(module_name), attr)
    if "calendar" not in inspect.signature(cls).parameters:
        return build_processor("metadata", config)
    from georg.modules.processors.metadata.calendar import load_calendar

    loaded = load_calendar(calendar.book, calendar.lectionary, calendar.occasions)
    return cls(dict(config.options), calendar=loaded)
