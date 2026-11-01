# SPDX-License-Identifier: AGPL-3.0-only
"""Transcriber for OpenAI-compatible `/audio/transcriptions` APIs (lemonfox.ai by default)."""

from georg.modules.processors.transcribe.chunks import merge_chunks, plan_chunks
from georg.modules.processors.transcribe.openai_compatible import (
    OpenAICompatibleTranscriber,
    TranscriberOptions,
    parse_verbose_json,
)

__all__ = [
    "OpenAICompatibleTranscriber",
    "TranscriberOptions",
    "merge_chunks",
    "parse_verbose_json",
    "plan_chunks",
]
