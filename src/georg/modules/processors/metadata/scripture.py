# SPDX-License-Identifier: AGPL-3.0-only
"""Which Scripture a sermon preaches on, from its transcript and the service bulletin.

Three methods, tried in order:

1. `text`: the sermon names its text with a reference ("the text for this morning is
   Ephesians 6:1-4", also spoken: "Ephesians chapter six, verses one through four").
2. `reading`: the sermon names its text as one of the bulletin's readings ("our text
   comes from the Gospel reading", "...from the book of Isaiah"); the bulletin gives
   the reference.
3. `llm`: a local llama.cpp model reads the labelled readings and the transcript and
   picks the readings the sermon is about (possibly several, possibly none). It shares
   the formatter's loaded model (`llama_cpp.shared_model`), so one copy is in memory.

Methods 1 and 2 fire only on statements about *the sermon's text*; passing mentions
("as St. Paul says in Philippians", "Genesis chapter 1") are left to method 3.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from georg.modules.processors.format.llama_cpp import (
    LlamaLike,
    LlamaModelOptions,
    reserve_context,
    shared_model,
)
from georg.modules.processors.format.prompt import clean_output
from georg.modules.processors.metadata.readings import (
    DEFAULT_HEADINGS,
    Reading,
    parse_readings,
)
from georg.modules.processors.metadata.references import find_books, find_references

log = logging.getLogger(__name__)

NAME = "scripture-2"
_NO_THINK = "/no_think"
_TEXT_STATEMENT = re.compile(
    r"""\b(?:the|our|today'?s|this\s+(?:morning|evening)'?s|my)\s+
    (?:(?:sermon|preaching|main|chief)\s+)?
    text
    (?:\s+(?:for|of)\s+(?:(?:this|our|today'?s|tonight'?s|the)(?:\s+[a-z']+){1,2}?|today|tonight))?
    (?:\s+(?:this\s+(?:morning|evening)|today|tonight))?
    ,?\s+(?:is|are|was|will\s+be|comes|come|came|is\s+taken|was\s+taken|is\s+drawn|is\s+found)
    (?:\s+(?:taken\s+)?(?:from|out\s+of|in))?\b""",
    re.IGNORECASE | re.VERBOSE,
)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_SAINT = re.compile(r"\bSt\.\s*", re.IGNORECASE)
_APOSTROPHE = str.maketrans({"\u2019": "'", "\u2018": "'"})
_WINDOW_CHARS = 200

Method = Literal["text", "reading", "llm"]


class ScriptureModelOptions(LlamaModelOptions):
    """The formatter's GGUF model settings, with room for a whole sermon."""

    context_size: int = Field(default=8192, ge=2048)
    max_transcript_words: int = Field(default=4000, ge=100)
    """Longer transcripts are cut to their first this-many words."""
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    @model_validator(mode="after")
    def _fits(self) -> "ScriptureModelOptions":
        if self.max_transcript_words * 8 // 5 + 1024 > self.context_size:
            raise ValueError("context_size is too small for max_transcript_words")
        return self


class ScriptureOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    headings: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_HEADINGS))
    """Bulletin heading -> reading label, e.g. {"Holy Gospel": "Gospel"}."""
    excerpt_chars: int = Field(default=500, ge=0)
    """How much of each reading's text the model sees."""
    llm: ScriptureModelOptions = Field(default_factory=ScriptureModelOptions)
    """Model for method 3 (the formatter's default Qwen3-4B GGUF)."""
    llm_enabled: bool = True
    """False skips method 3, so only stated texts are detected."""
    results_dir: Path | None = None
    """Where the model's picks are cached (by model, transcript and readings)."""
    prompt_version: str = Field(default="v1", pattern=r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class ScriptureResult:
    references: tuple[str, ...]
    method: Method
    reason: str
    """One line saying why, for the review page."""

    @property
    def origin(self) -> str:
        return f"{NAME}/{self.method}: {self.reason}"


def _quote(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return f'"{text}"' if len(text) <= limit else f'"{text[: limit - 1].rstrip()}\u2026"'


def _normalize(text: str) -> str:
    return _SAINT.sub("Saint ", text.translate(_APOSTROPHE))


@dataclass(frozen=True)
class _Statement:
    references: tuple[str, ...]
    method: Method
    quote: str
    reading: Reading | None = None


def text_statements(transcript: str, readings: Sequence[Reading]) -> list[_Statement]:
    """Methods 1 and 2: every statement of the sermon's text that names a passage."""
    text = _normalize(transcript)
    found: list[_Statement] = []
    for match in _TEXT_STATEMENT.finditer(text):
        tail = text[match.end() : match.end() + _WINDOW_CHARS]
        end = _SENTENCE_END.search(tail)
        window = tail[: end.start() + 1] if end else tail
        quote = _quote(text[match.start() : match.end() + len(window)])
        refs = find_references(window)
        if refs:
            found.append(_Statement((refs[0][0],), "text", quote))
            continue
        reading = _named_reading(window, readings)
        if reading is not None:
            found.append(_Statement((reading.reference,), "reading", quote, reading))
    return found


def _named_reading(window: str, readings: Sequence[Reading]) -> Reading | None:
    lowered = window.lower()
    best: tuple[int, Reading] | None = None
    for reading in readings:
        for name in reading.spoken_names:
            m = re.search(rf"\b{re.escape(name)}\b", lowered)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), reading)
    if best is not None:
        return best[1]
    for mention in find_books(window):
        for reading in readings:
            if reading.book == mention.book:
                return reading
    return None


class ScriptureDetector:
    def __init__(
        self,
        options: ScriptureOptions,
        *,
        model_factory: Callable[[LlamaModelOptions], LlamaLike] | None = None,
    ) -> None:
        self.options = options
        self._factory = model_factory
        if options.llm_enabled:
            reserve_context(options.llm, model_factory)
        self._template = Template(
            resources.files(__package__)
            .joinpath("prompts", f"scripture_{options.prompt_version}.txt")
            .read_text("utf-8")
        )
        llm = options.llm if options.llm_enabled else None
        self.version = NAME + (
            f"-{llm.model_name}-prompt-{options.prompt_version}-c{llm.context_size}"
            f"-w{llm.max_transcript_words}-t{llm.temperature:g}-s{llm.seed}"
            f"-e{options.excerpt_chars}"
            if llm
            else "-nollm"
        )

    def readings(self, documents: Mapping[str, str]) -> list[Reading]:
        """The readings of the first document (in the given order) that lists any."""
        for text in documents.values():
            found = parse_readings(text, self.options.headings, self.options.excerpt_chars)
            if found:
                return found
        return []

    def detect(self, transcript: str, documents: Mapping[str, str]) -> ScriptureResult | None:
        """The sermon's Scripture, or None (with a logged reason). Never raises."""
        if not transcript.strip():
            log.warning("scripture: the sermon transcript is empty")
            return None
        readings = self.readings(documents)
        statements = text_statements(transcript, readings)
        if statements:
            refs = tuple(dict.fromkeys(r for s in statements for r in s.references))
            first = statements[0]
            if all(s.method == "text" for s in statements):
                return ScriptureResult(refs, "text", f"sermon says {first.quote}")
            reading = next(s.reading for s in statements if s.reading is not None)
            return ScriptureResult(
                refs,
                "reading",
                f"sermon says {first.quote}; bulletin {reading.heading} is {reading.reference}",
            )
        if not readings:
            log.warning("scripture: no text statement and no readings in the documents")
            return None
        if not self.options.llm_enabled:
            log.info("scripture: no text statement and the model is disabled")
            return None
        try:
            labels = self._pick(transcript, readings)
        except Exception as exc:
            log.warning("scripture: the model failed: %s", exc)
            return None
        chosen = [r for label in labels for r in readings if r.label == label]
        if not chosen:
            log.info("scripture: the model picked none of %d readings", len(readings))
            return None
        names = ", ".join(r.label for r in chosen)
        offered = ", ".join(r.label for r in readings)
        return ScriptureResult(
            tuple(r.reference for r in chosen),
            "llm",
            f"local model picked {names} from the bulletin readings ({offered})",
        )

    def _pick(self, transcript: str, readings: Sequence[Reading]) -> list[str]:
        llm = self.options.llm
        words = transcript.split()
        text = " ".join(words[: llm.max_transcript_words])
        if len(words) > llm.max_transcript_words:
            text += " [...]"
        listing = "\n\n".join(f"[{r.label}] {r.reference}\n{r.text}".rstrip() for r in readings)
        key = hashlib.sha256(
            json.dumps([self.version, text, listing], ensure_ascii=False).encode()
        ).hexdigest()[:32]
        cached = self._cached(key)
        if cached is not None:
            return cached
        reply = self._complete(self._template.safe_substitute(readings=listing, transcript=text),
                               [r.label for r in readings])  # fmt: skip
        labels = parse_labels(reply, [r.label for r in readings])
        self._store(key, labels)
        return labels

    def _cached(self, key: str) -> list[str] | None:
        if self.options.results_dir is None:
            return None
        path = self.options.results_dir / f"{key}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else None

    def _store(self, key: str, labels: list[str]) -> None:
        if self.options.results_dir is None:
            return
        path = self.options.results_dir / f"{key}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.partial")
            tmp.write_text(json.dumps(labels), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("scripture: could not cache the model's pick: %s", exc)

    def _complete(self, prompt: str, labels: list[str]) -> str:
        llm = self.options.llm
        model = shared_model(llm, self._factory)
        messages: list[dict[str, str]] = []
        if llm.disable_thinking:
            messages.append({"role": "system", "content": _NO_THINK})
        messages.append({"role": "user", "content": prompt})
        schema = {
            "type": "object",
            "properties": {
                "readings": {"type": "array", "items": {"type": "string", "enum": labels}}
            },
            "required": ["readings"],
        }
        result: Any = model.create_chat_completion(
            messages,
            temperature=llm.temperature,
            seed=llm.seed,
            max_tokens=32 + 16 * len(labels),
            response_format={"type": "json_object", "schema": schema},
        )
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("chat response has no text content")
        return content


def parse_labels(reply: str, labels: Sequence[str]) -> list[str]:
    """Known labels from a reply like {"readings": ["Gospel"]}, in order; others dropped."""
    text = clean_output(reply, "")
    try:
        value: Any = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        try:
            value = json.loads(match.group(0)) if match else None
        except ValueError:
            value = None
    if isinstance(value, dict):
        value = value.get("readings")
    if not isinstance(value, list):
        log.warning("scripture: unusable model reply %r", reply[:200])
        return []
    known = {label.casefold(): label for label in labels}
    picked: list[str] = []
    for item in value:
        label = known.get(item.strip().casefold()) if isinstance(item, str) else None
        if label is None:
            log.info("scripture: dropping unknown reading label %r", item)
        elif label not in picked:
            picked.append(label)
    return picked
