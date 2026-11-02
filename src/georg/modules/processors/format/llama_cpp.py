# SPDX-License-Identifier: AGPL-3.0-only
"""`TranscriptFormatter` running an open-weights GGUF chat model locally with llama.cpp.

llama-cpp-python is an optional extra (`georg[llm]`) imported when the first transcript
is formatted. The model is downloaded from Hugging Face on first use into `cache_dir`
(default: the Hugging Face cache) unless `model_path` points at a local GGUF file; after
that no network access is needed.

The transcript is split at sentence ends into chunks of `min_chunk_words`-`max_chunk_words`
words. In the default `breaks` mode the model sees a chunk's numbered sentences and
replies only with the numbers that start paragraphs, so the output is rebuilt from the
original sentences and no word can change; the last paragraph of each chunk is carried
into the next one as context, so chunk ends are not forced paragraph breaks. In
`rewrite` mode the model returns the formatted text using the `openai_chat` prompt
template, and each chunk is checked with `compare_word_sequences`.

A chunk whose reply is unusable (unparseable numbers, or changed words) is retried once
at `retry_temperature`; if that fails too its original text is used unformatted and a
warning is added to `discrepancies`.
"""

import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, cast

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
_BREAKS_REPLY = re.compile(r"[\d\s,;\[\]]*")


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
    mode: Literal["breaks", "rewrite"] = "breaks"
    """`breaks`: the model only picks paragraph starts. `rewrite`: it returns the text."""
    min_chunk_words: int = Field(default=400, gt=0)
    max_chunk_words: int = Field(default=700, gt=0)
    disable_thinking: bool = True
    """Send Qwen3's `/no_think` switch and drop any `<think>` block from the reply."""
    prompt_version: str = Field(default="v1", pattern=r"^[a-z0-9_-]+$")
    prompt_file: Path | None = None
    """A custom template using `$text` and `$language`; overrides `prompt_version`.

    `breaks` mode loads `prompts/breaks_<version>.txt`, `rewrite` mode
    `prompts/format_<version>.txt` (shared with `openai_chat`)."""

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
        stem = "breaks" if o.mode == "breaks" else "format"
        self._template, prompt_id = load_template(o.prompt_version, o.prompt_file, stem)
        self._factory = model_factory
        self._model: LlamaLike | None = None
        model = o.model_path.name if o.model_path else o.model_file
        self.version = (
            f"{TYPE_NAME}-r{_REVISION}-{o.mode}-{model}-prompt-{prompt_id}-t{o.temperature:g}"
            f"-rt{o.retry_temperature:g}-s{o.seed}-w{o.min_chunk_words}-{o.max_chunk_words}"
            f"-nt{int(o.disable_thinking)}"
        )

    def format(self, transcript: Transcript, ctx: FormatContext) -> FormattedTranscript:
        o = self.options
        chunks = split_sentences(transcript.text, o.min_chunk_words, o.max_chunk_words)
        if not chunks:
            return FormattedTranscript(markdown="", words_preserved=True)
        if o.mode == "breaks":
            parts, warnings = self._format_breaks(chunks, ctx.language)
        else:
            parts, warnings = self._format_rewrite(chunks, ctx.language)
        markdown = "\n\n".join(parts)
        check = compare_word_sequences(transcript.text, markdown)
        return FormattedTranscript(
            markdown=markdown,
            words_preserved=check.preserved,
            discrepancies=(*warnings, *check.discrepancies),
        )

    def _format_rewrite(self, chunks: list[str], language: str) -> tuple[list[str], list[str]]:
        parts: list[str] = []
        warnings: list[str] = []
        position = 1
        for index, chunk in enumerate(chunks, start=1):
            formatted = self._rewrite_chunk(chunk, language)
            words = len(chunk.split())
            if formatted is None:
                warnings.append(
                    _warning(index, len(chunks), position, words, "the model changed words")
                )
                formatted = chunk
            parts.append(formatted)
            position += words
        return parts, warnings

    def _rewrite_chunk(self, text: str, language: str) -> str | None:
        """The formatted chunk, or None if the model changed its words twice."""
        messages = self._messages(text, language)
        max_tokens = min(len(text.split()) * 3 + 128, self.options.context_size // 2)
        for temperature, seed in self._attempts():
            reply = self._complete(messages, temperature, seed, max_tokens)
            formatted = clean_output(reply, text)
            if formatted and compare_word_sequences(text, formatted).preserved:
                return formatted
        return None

    def _format_breaks(self, chunks: list[str], language: str) -> tuple[list[str], list[str]]:
        paragraphs: list[list[str]] = []
        warnings: list[str] = []
        carry: list[str] = []
        position = 1
        for index, chunk in enumerate(chunks, start=1):
            sentences = [" ".join(s) for s in _sentences(chunk)]
            window = carry + sentences
            found = self._paragraphs(window, language)
            words = len(chunk.split())
            if found is None:
                warnings.append(
                    _warning(index, len(chunks), position, words, "the model gave no usable breaks")
                )
                paragraphs.extend(p for p in (carry, sentences) if p)
                carry = []
            else:
                *done, last = found
                paragraphs.extend(done)
                carry_words = sum(len(s.split()) for s in last)
                if done and carry_words <= self.options.max_chunk_words // 2:
                    carry = last
                else:
                    paragraphs.append(last)
                    carry = []
            position += words
        if carry:
            paragraphs.append(carry)
        return [" ".join(p) for p in paragraphs], warnings

    def _paragraphs(self, sentences: list[str], language: str) -> list[list[str]] | None:
        """`sentences` grouped into paragraphs, or None if both replies were unusable."""
        if len(sentences) < 2:
            return [sentences]
        numbered = "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences, start=1))
        messages = self._messages(numbered, language)
        max_tokens = min(len(sentences) * 4 + 32, self.options.context_size // 2)
        short = sum(len(s.split()) for s in sentences) < self.options.min_chunk_words
        for temperature, seed in self._attempts():
            reply = clean_output(self._complete(messages, temperature, seed, max_tokens), "")
            starts = _parse_starts(reply, len(sentences))
            if starts is not None and (starts or short):
                return _group(sentences, starts)
        return None

    def _attempts(self) -> tuple[tuple[float, int], tuple[float, int]]:
        o = self.options
        return (o.temperature, o.seed), (o.retry_temperature, o.seed + 1)

    def _messages(self, text: str, language: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if self.options.disable_thinking:
            messages.append({"role": "system", "content": _NO_THINK})
        prompt = self._template.safe_substitute(text=text, language=language)
        messages.append({"role": "user", "content": prompt})
        return messages

    def _complete(
        self, messages: Sequence[dict[str, str]], temperature: float, seed: int, max_tokens: int
    ) -> str:
        if self._model is None:
            self._model = (self._factory or _load_model)(self.options)
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


def _parse_starts(reply: str, count: int) -> set[int] | None:
    """Sentence numbers (2..count) from a reply like `4, 9, 13`; None if it isn't one."""
    reply = reply.strip().rstrip(".")
    if not _BREAKS_REPLY.fullmatch(reply):
        return None
    numbers = {int(n) for n in re.findall(r"\d+", reply)}
    if any(n < 1 or n > count for n in numbers):
        return None
    return numbers - {1}


def _group(sentences: list[str], starts: set[int]) -> list[list[str]]:
    paragraphs: list[list[str]] = []
    for number, sentence in enumerate(sentences, start=1):
        if number == 1 or number in starts:
            paragraphs.append([])
        paragraphs[-1].append(sentence)
    return paragraphs


def _warning(index: int, total: int, position: int, words: int, reason: str) -> str:
    return (
        f"chunk {index} of {total} (words {position}-{position + words - 1}):"
        f" {reason}, so this part is left unformatted"
    )
