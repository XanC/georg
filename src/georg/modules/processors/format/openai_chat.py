# SPDX-License-Identifier: AGPL-3.0-only
"""`TranscriptFormatter` that asks a chat model to add paragraphs and punctuation.

The prompt is a versioned template under `prompts/` (ported from the original
`format_sermons.pl`). Long transcripts are formatted in chunks of whole segments. Each
chunk's output is checked with `compare_word_sequences`; a chunk whose words changed is
retried once, and if it still differs the result is returned with
`words_preserved=False` and the differences listed for the reviewer.
"""

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from georg.core.config import resolve_secret
from georg.core.errors import ConfigError
from georg.core.types import FormatContext, FormattedTranscript, Transcript
from georg.llm.chat import ChatClient
from georg.modules.processors.format.prompt import clean_output, load_template
from georg.modules.processors.format.words import compare_word_sequences

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class FormatterOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: str
    """API key, normally an "env:NAME" reference."""
    base_url: str = DEFAULT_BASE_URL
    model: str = "gpt-4o-mini"
    temperature: float | None = Field(default=0.0, ge=0.0, le=2.0)
    prompt_version: str = Field(default="v1", pattern=r"^[a-z0-9_-]+$")
    prompt_file: Path | None = None
    """A custom template using `$text` and `$language`; overrides `prompt_version`."""
    max_chunk_words: int = Field(default=1500, gt=0)
    timeout_s: float = Field(default=180.0, gt=0)
    attempts: int = Field(default=3, ge=1)
    """HTTP attempts per request (transient errors only)."""


def split_for_formatting(transcript: Transcript, max_words: int) -> list[str]:
    """Group segment texts into chunks of at most `max_words` words (larger segments split)."""
    pieces: list[list[str]] = []
    for segment in transcript.segments:
        words = segment.text.split()
        pieces.extend(words[i : i + max_words] for i in range(0, len(words), max_words))
    chunks: list[list[str]] = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + len(piece) <= max_words:
            chunks[-1].extend(piece)
        else:
            chunks.append(list(piece))
    return [" ".join(chunk) for chunk in chunks]


class OpenAIChatFormatter:
    """Module type `openai_chat`."""

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            self.options = FormatterOptions.model_validate(dict(options))
        except ValidationError as exc:
            raise ConfigError(f"formatter options: {exc}") from exc
        self._template, prompt_id = load_template(
            self.options.prompt_version, self.options.prompt_file
        )
        self.version = f"openai_chat-1+prompt-{prompt_id}"
        self._chat = ChatClient(
            base_url=self.options.base_url,
            api_key=resolve_secret(self.options.api_key),
            provider="formatter",
            timeout_s=self.options.timeout_s,
            attempts=self.options.attempts,
            http_client=http_client,
            sleep=sleep,
        )

    def format(self, transcript: Transcript, ctx: FormatContext) -> FormattedTranscript:
        chunks = split_for_formatting(transcript, self.options.max_chunk_words)
        if not chunks:
            return FormattedTranscript(markdown="", words_preserved=True)
        markdown = "\n\n".join(self._format_chunk(chunk, ctx.language) for chunk in chunks)
        check = compare_word_sequences(transcript.text, markdown)
        return FormattedTranscript(
            markdown=markdown,
            words_preserved=check.preserved,
            discrepancies=check.discrepancies,
        )

    def _format_chunk(self, text: str, language: str) -> str:
        prompt = self._template.safe_substitute(text=text, language=language)
        formatted = text
        for _ in range(2):
            result = self._chat.complete(
                [{"role": "user", "content": prompt}],
                model=self.options.model,
                temperature=self.options.temperature,
            )
            formatted = clean_output(result.content, text)
            if compare_word_sequences(text, formatted).preserved:
                break
        return formatted
