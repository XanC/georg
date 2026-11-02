# SPDX-License-Identifier: AGPL-3.0-only
"""`TranscriptFormatter` running an open-weights GGUF chat model locally with llama.cpp.

llama-cpp-python is an optional extra (`georg[llm]`) imported when the first transcript
is formatted. The model is downloaded from Hugging Face on first use into `cache_dir`
(default: the Hugging Face cache) unless `model_path` points at a local GGUF file; after
that no network access is needed.

The prompt is the same versioned template the `openai_chat` formatter uses. The
transcript is split at sentence ends into chunks of `min_chunk_words`-`max_chunk_words`
words. Each chunk's output is checked with `compare_word_sequences`; a chunk whose words
changed is retried once at `retry_temperature`, and if it still differs the chunk's
original text is used unformatted and a warning is added to `discrepancies`.
"""

import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from georg.core.errors import ConfigError, ModuleError
from georg.core.types import FormatContext, FormattedTranscript, Transcript
from georg.modules.processors.format.prompt import clean_output, load_template
from georg.modules.processors.format.words import compare_word_sequences

TYPE_NAME = "llama_cpp"
_REVISION = 1
DEFAULT_MODEL_REPO = "unsloth/Qwen3-4B-Instruct-2507-GGUF"
DEFAULT_MODEL_FILE = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
_SENTENCE_END = re.compile(r"[.?!][\"'\u201d\u2019\u00bb)\]]*$")
_NO_THINK = "/no_think"


class LlamaLike(Protocol):
    """The subset of `llama_cpp.Llama` that Georg uses."""

    def create_chat_completion(self, messages: Any, **kwargs: Any) -> Any: ...


class LlamaFormatterOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_repo: str = DEFAULT_MODEL_REPO
    """Hugging Face repository holding the GGUF file."""
    model_file: str = DEFAULT_MODEL_FILE
    model_path: Path | None = None
    """A local GGUF file; overrides `model_repo`/`model_file` and never downloads."""
    cache_dir: Path | None = None
    """Download directory; None uses the Hugging Face cache (`$HF_HOME`)."""
    local_files_only: bool = False
    threads: int = Field(default=0, ge=0)
    """CPU threads; 0 lets llama.cpp choose."""
    context_size: int = Field(default=4096, ge=1024)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    retry_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    """Used for the single retry of a chunk whose words changed."""
    seed: int = Field(default=0, ge=0)
    min_chunk_words: int = Field(default=400, gt=0)
    max_chunk_words: int = Field(default=700, gt=0)
    disable_thinking: bool = True
    """Send Qwen3's `/no_think` switch and drop any `<think>` block from the reply."""
    prompt_version: str = Field(default="v1", pattern=r"^[a-z0-9_-]+$")
    prompt_file: Path | None = None
    """A custom template using `$text` and `$language`; overrides `prompt_version`."""

    @model_validator(mode="after")
    def _chunk_range(self) -> "LlamaFormatterOptions":
        if self.min_chunk_words > self.max_chunk_words:
            raise ValueError("min_chunk_words must not exceed max_chunk_words")
        if self.max_chunk_words * 4 > self.context_size:
            raise ValueError("context_size must be at least 4 tokens per max_chunk_words")
        return self


def model_file_path(options: LlamaFormatterOptions) -> Path:
    """The local GGUF path, downloading it first if needed."""
    if options.model_path is not None:
        if not options.model_path.is_file():
            raise ModuleError(f"model file not found: {options.model_path}")
        return options.model_path
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise ModuleError(
            "huggingface-hub is not installed; install the 'llm' extra (uv sync --extra llm)"
        ) from exc
    try:
        path = hf_hub_download(
            repo_id=options.model_repo,
            filename=options.model_file,
            cache_dir=str(options.cache_dir) if options.cache_dir else None,
            local_files_only=options.local_files_only,
        )
    except Exception as exc:
        raise ModuleError(
            f"could not download {options.model_repo}/{options.model_file}: {exc}",
            retryable=True,
        ) from exc
    return Path(path)


def _load_model(options: LlamaFormatterOptions) -> LlamaLike:
    # Spinning OpenMP workers make generation several times slower on shared vCPUs.
    os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise ModuleError(
            "llama-cpp-python is not installed; install the 'llm' extra (uv sync --extra llm)"
        ) from exc
    path = model_file_path(options)
    try:
        model = cast(
            LlamaLike,
            Llama(
                model_path=str(path),
                n_ctx=options.context_size,
                n_threads=options.threads or None,
                seed=options.seed,
                verbose=False,
            ),
        )
    except Exception as exc:
        raise ModuleError(f"could not load model {path}: {exc}") from exc
    return model


def _sentences(text: str) -> list[list[str]]:
    sentences: list[list[str]] = [[]]
    for word in text.split():
        sentences[-1].append(word)
        if _SENTENCE_END.search(word):
            sentences.append([])
    return [s for s in sentences if s]


def split_sentences(text: str, min_words: int, max_words: int) -> list[str]:
    """Group whole sentences into chunks of `min_words`-`max_words` words where possible.

    A sentence longer than `max_words` is split; a chunk below `min_words` grows past
    `max_words` only by the one sentence that would otherwise leave it short.
    """
    pieces: list[list[str]] = []
    for sentence in _sentences(text):
        pieces.extend(sentence[i : i + max_words] for i in range(0, len(sentence), max_words))
    chunks: list[list[str]] = []
    for piece in pieces:
        if chunks and (len(chunks[-1]) + len(piece) <= max_words or len(chunks[-1]) < min_words):
            chunks[-1].extend(piece)
        else:
            chunks.append(list(piece))
    if (
        len(chunks) > 1
        and len(chunks[-1]) < min_words
        and len(chunks[-2]) + len(chunks[-1]) <= max_words + min_words // 2
    ):
        chunks[-2].extend(chunks.pop())
    return [" ".join(chunk) for chunk in chunks]


class LlamaCppFormatter:
    """Module type `llama_cpp`: formats transcripts on this machine's CPU."""

    type_name = TYPE_NAME

    def __init__(
        self,
        options: Mapping[str, Any] | None = None,
        *,
        model_factory: Callable[[LlamaFormatterOptions], LlamaLike] | None = None,
    ) -> None:
        try:
            self.options = LlamaFormatterOptions.model_validate(dict(options or {}))
        except ValidationError as exc:
            raise ConfigError(f"{TYPE_NAME}: {exc}") from exc
        o = self.options
        self._template, prompt_id = load_template(o.prompt_version, o.prompt_file)
        self._factory = model_factory
        self._model: LlamaLike | None = None
        model = o.model_path.name if o.model_path else o.model_file
        self.version = (
            f"{TYPE_NAME}-r{_REVISION}-{model}-prompt-{prompt_id}-t{o.temperature:g}"
            f"-rt{o.retry_temperature:g}-s{o.seed}-w{o.min_chunk_words}-{o.max_chunk_words}"
            f"-nt{int(o.disable_thinking)}"
        )

    def format(self, transcript: Transcript, ctx: FormatContext) -> FormattedTranscript:
        o = self.options
        chunks = split_sentences(transcript.text, o.min_chunk_words, o.max_chunk_words)
        if not chunks:
            return FormattedTranscript(markdown="", words_preserved=True)
        warnings: list[str] = []
        parts: list[str] = []
        position = 1
        for index, chunk in enumerate(chunks, start=1):
            formatted = self._format_chunk(chunk, ctx.language)
            words = len(chunk.split())
            if formatted is None:
                warnings.append(
                    f"chunk {index} of {len(chunks)} (words {position}-{position + words - 1}):"
                    " the model changed words, so this part is left unformatted"
                )
                formatted = chunk
            parts.append(formatted)
            position += words
        markdown = "\n\n".join(parts)
        check = compare_word_sequences(transcript.text, markdown)
        return FormattedTranscript(
            markdown=markdown,
            words_preserved=check.preserved,
            discrepancies=(*warnings, *check.discrepancies),
        )

    def _format_chunk(self, text: str, language: str) -> str | None:
        """The formatted chunk, or None if the model changed its words twice."""
        o = self.options
        messages: list[dict[str, str]] = []
        if o.disable_thinking:
            messages.append({"role": "system", "content": _NO_THINK})
        prompt = self._template.safe_substitute(text=text, language=language)
        messages.append({"role": "user", "content": prompt})
        for temperature, seed in ((o.temperature, o.seed), (o.retry_temperature, o.seed + 1)):
            formatted = clean_output(self._complete(messages, temperature, seed, text), text)
            if formatted and compare_word_sequences(text, formatted).preserved:
                return formatted
        return None

    def _complete(
        self, messages: Sequence[dict[str, str]], temperature: float, seed: int, text: str
    ) -> str:
        if self._model is None:
            self._model = (self._factory or _load_model)(self.options)
        max_tokens = min(len(text.split()) * 3 + 128, self.options.context_size // 2)
        try:
            result = self._model.create_chat_completion(
                list(messages), temperature=temperature, seed=seed, max_tokens=max_tokens
            )
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModuleError(f"{TYPE_NAME}: unexpected chat response shape") from exc
        except Exception as exc:
            raise ModuleError(f"{TYPE_NAME}: model failed: {exc}") from exc
        if not isinstance(content, str):
            raise ModuleError(f"{TYPE_NAME}: chat response has no text content")
        return content
