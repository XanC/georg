# SPDX-License-Identifier: AGPL-3.0-only
"""Transcribers: local Whisper (faster-whisper) and OpenAI-compatible APIs such as lemonfox.ai."""

from georg.modules.processors.transcribe.chunks import merge_chunks, plan_chunks
from georg.modules.processors.transcribe.faster_whisper import (
    FasterWhisperOptions,
    FasterWhisperTranscriber,
)
from georg.modules.processors.transcribe.openai_compatible import (
    OpenAICompatibleTranscriber,
    TranscriberOptions,
    parse_verbose_json,
)

__all__ = [
    "FasterWhisperOptions",
    "FasterWhisperTranscriber",
    "OpenAICompatibleTranscriber",
    "TranscriberOptions",
    "merge_chunks",
    "parse_verbose_json",
    "plan_chunks",
]
