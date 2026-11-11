# SPDX-License-Identifier: AGPL-3.0-only
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from georg.core.config import ModuleConfig, ProcessorsConfig, load_config
from georg.core.contracts import TranscriptFormatter
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import (
    FormatContext,
    SermonMetadata,
    TimeRange,
    Transcript,
    TranscriptSegment,
)
from georg.modules.processors.format import llama_cpp
from georg.modules.processors.format.llama_cpp import (
    LlamaCppFormatter,
    LlamaFormatterOptions,
    split_sentences,
)
from georg.pipeline import build_processor, build_processors

CTX = FormatContext(language="en", metadata=SermonMetadata())


def transcript(*texts: str) -> Transcript:
    segments = tuple(
        TranscriptSegment(text=t, range=TimeRange(start_ms=i * 1000, end_ms=(i + 1) * 1000))
        for i, t in enumerate(texts)
    )
    return Transcript(language="en", segments=segments, provider="test")


def chunk_of(messages: list[dict[str, str]]) -> str:
    return re.split(r"(?:Text|Sentences):\n", messages[-1]["content"], maxsplit=1)[1].strip()


class FakeLlama:
    """Replies via `reply(chunk, call_number)`; records every call."""

    def __init__(self, reply: Any = None) -> None:
        self.reply = reply or (lambda chunk, n: chunk)
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        content = self.reply(chunk_of(messages), len(self.calls))
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def make(fake: FakeLlama, **options: Any) -> LlamaCppFormatter:
    return LlamaCppFormatter({"mode": "rewrite", **options}, model_factory=lambda _: fake)


def breaks(fake: FakeLlama, **options: Any) -> LlamaCppFormatter:
    return LlamaCppFormatter(options, model_factory=lambda _: fake)


def sentences(n: int, words: int = 10, start: int = 0) -> str:
    return " ".join(
        " ".join(f"w{start + i}x{j}" for j in range(words - 1)) + f" end{start + i}."
        for i in range(n)
    )


def test_formats_with_shared_prompt_and_settings() -> None:
    fake = FakeLlama(lambda chunk, n: "Grace to you.\n\nAnd peace!")
    formatter = make(fake)
    assert isinstance(formatter, TranscriptFormatter)
    result = formatter.format(transcript("grace to you", "and peace"), CTX)
    assert result.markdown == "Grace to you.\n\nAnd peace!"
    assert result.words_preserved and result.discrepancies == ()
    call = fake.calls[0]
    assert call["temperature"] == 0.0 and call["seed"] == 0
    assert call["messages"][0] == {"role": "system", "content": "/no_think"}
    prompt = call["messages"][-1]["content"]
    assert "Do NOT change any words" in prompt and "(en)" in prompt
    assert chunk_of(call["messages"]) == "grace to you and peace"
    assert formatter.version.startswith(
        "llama_cpp-r1-rewrite-Qwen3-4B-Instruct-2507-Q4_K_M.gguf-prompt-v1"
    )


def test_thinking_block_and_fence_are_dropped() -> None:
    fake = FakeLlama(lambda chunk, n: "<think>\nhmm\n</think>\n\n```\nGrace to you.\n```")
    result = make(fake).format(transcript("grace to you"), CTX)
    assert result.markdown == "Grace to you." and result.words_preserved


def test_thinking_switch_can_be_turned_off() -> None:
    fake = FakeLlama()
    make(fake, disable_thinking=False).format(transcript("Amen."), CTX)
    assert [m["role"] for m in fake.calls[0]["messages"]] == ["user"]


def test_chunks_at_sentence_ends_within_limits() -> None:
    text = sentences(130)  # 1300 words in 10-word sentences
    chunks = split_sentences(text, 400, 700)
    assert [len(c.split()) for c in chunks] == [700, 600]
    assert all(c.endswith(".") for c in chunks)
    assert " ".join(chunks) == text


def test_short_tail_is_merged_and_long_sentence_split() -> None:
    assert len(split_sentences(sentences(45), 400, 420)) == 1
    long_sentence = " ".join(f"w{i}" for i in range(1000)) + "."
    chunks = split_sentences(long_sentence, 100, 400)
    assert [len(c.split()) for c in chunks] == [400, 400, 200]
    assert split_sentences("   ", 400, 700) == []


def test_closing_quotes_end_sentences() -> None:
    text = 'He said, "Go." ' + sentences(2)
    assert split_sentences(text, 1, 3)[0] == 'He said, "Go."'


def test_each_chunk_is_formatted_and_joined() -> None:
    fake = FakeLlama(lambda chunk, n: chunk.replace(" end", "\n\nend", 1))
    result = make(fake, min_chunk_words=100, max_chunk_words=200).format(
        transcript(sentences(20), sentences(20, start=20)), CTX
    )
    assert len(fake.calls) == 2
    assert result.words_preserved and result.discrepancies == ()
    assert result.markdown.count("\n\n") == 3


def test_changed_words_are_retried_once_with_other_settings() -> None:
    fake = FakeLlama(lambda chunk, n: "Grace and peace." if n == 1 else "Grace to you.")
    result = make(fake).format(transcript("grace to you"), CTX)
    assert result.markdown == "Grace to you." and result.discrepancies == ()
    assert [(c["temperature"], c["seed"]) for c in fake.calls] == [(0.0, 0), (0.3, 1)]


def test_chunk_failing_twice_keeps_original_text_with_warning() -> None:
    def reply(chunk: str, n: int) -> str:
        return chunk.replace("w0x1", "changed") if "w0x1" in chunk else chunk.upper()

    fake = FakeLlama(reply)
    source = transcript(sentences(20), sentences(20, start=20))
    result = make(fake, min_chunk_words=100, max_chunk_words=200).format(source, CTX)
    assert len(fake.calls) == 3
    first, second = result.markdown.split("\n\n")
    assert first == sentences(20) and second == sentences(20, start=20).upper()
    assert "changed" not in result.markdown
    assert result.words_preserved
    assert result.discrepancies == (
        "chunk 1 of 2 (words 1-200): the model changed words, so this part is left unformatted",
    )


def test_empty_reply_counts_as_failure() -> None:
    result = make(FakeLlama(lambda chunk, n: "  ")).format(transcript("grace to you"), CTX)
    assert result.markdown == "grace to you" and len(result.discrepancies) == 1


def test_empty_transcript_does_not_load_the_model() -> None:
    def factory(_: LlamaFormatterOptions) -> FakeLlama:
        raise AssertionError("model loaded")

    result = LlamaCppFormatter({}, model_factory=factory).format(transcript(), CTX)
    assert result.markdown == "" and result.words_preserved


def test_model_is_loaded_once() -> None:
    loads: list[LlamaFormatterOptions] = []

    def factory(options: LlamaFormatterOptions) -> FakeLlama:
        loads.append(options)
        return FakeLlama()

    formatter = LlamaCppFormatter({"threads": 4, "mode": "rewrite"}, model_factory=factory)
    formatter.format(transcript("Amen."), CTX)
    formatter.format(transcript("Amen."), CTX)
    assert len(loads) == 1 and loads[0].threads == 4


def test_bad_response_shape_is_module_error() -> None:
    class Broken:
        def create_chat_completion(self, messages: Any, **kwargs: Any) -> Any:
            return {"choices": []}

    formatter = LlamaCppFormatter({"mode": "rewrite"}, model_factory=lambda _: Broken())
    with pytest.raises(ModuleError, match="unexpected chat response shape"):
        formatter.format(transcript("Amen."), CTX)


@pytest.mark.parametrize(
    "options",
    [
        {"min_chunk_words": 800, "max_chunk_words": 700},
        {"max_chunk_words": 2000, "context_size": 4096},
        {"temperature": -1},
        {"threads": -1},
        {"context_size": 512},
        {"prompt_version": "../x"},
        {"modle_file": "typo.gguf"},
        {"mode": "paraphrase"},
    ],
)
def test_invalid_options_are_config_errors(options: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        LlamaCppFormatter(options, model_factory=lambda _: FakeLlama())


def test_version_tracks_model_and_settings() -> None:
    base = LlamaCppFormatter({}).version
    assert LlamaCppFormatter({"model_path": "/m/other.gguf"}).version != base
    assert LlamaCppFormatter({"max_chunk_words": 600}).version != base
    assert LlamaCppFormatter({"mode": "rewrite"}).version != base
    assert LlamaCppFormatter({"threads": 8}).version == base


def test_missing_local_model_file_is_module_error(tmp_path: Path) -> None:
    options = LlamaFormatterOptions(model_path=tmp_path / "missing.gguf")
    with pytest.raises(ModuleError, match="model file not found"):
        llama_cpp.model_file_path(options)


def test_registered_and_default_in_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert ProcessorsConfig(
        classifier=ModuleConfig(type="c"),
        detector=ModuleConfig(type="d"),
        transcriber=ModuleConfig(type="t"),
        metadata=ModuleConfig(type="m"),
    ).formatter == ModuleConfig(type="llama_cpp")
    path = tmp_path / "georg.toml"
    path.write_text(
        f'instance = "t"\ndata_dir = "{tmp_path}"\n'
        '[[inputs]]\nname = "drive"\ntype = "local_folder"\n[processors]\n'
        'classifier = { type = "ina_speech_segmenter" }\ndetector = { type = "heuristic" }\n'
        'transcriber = { type = "faster_whisper" }\nmetadata = { type = "rules" }\n'
    )
    fake = FakeLlama(lambda chunk, n: "6")
    monkeypatch.setattr(llama_cpp, "load_model", lambda _: fake)
    formatter = build_processors(load_config(path).processors).formatter
    assert isinstance(formatter, LlamaCppFormatter) and formatter.options.mode == "breaks"
    result = formatter.format(transcript(sentences(5), sentences(5, start=5)), CTX)
    assert result.markdown == sentences(5) + "\n\n" + sentences(5, start=5)
    assert result.discrepancies == ()


def test_build_processor_by_type() -> None:
    formatter = build_processor("formatter", ModuleConfig(type="llama_cpp", options={"seed": 7}))
    assert isinstance(formatter, LlamaCppFormatter) and formatter.options.seed == 7


def test_load_model_prefers_passive_openmp_and_needs_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMP_WAIT_POLICY", raising=False)
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    with pytest.raises(ModuleError, match="llm"):
        llama_cpp.load_model(LlamaFormatterOptions())
    assert os.environ["OMP_WAIT_POLICY"] == "PASSIVE"


def paragraphs_of(text: str) -> list[list[str]]:
    return [re.findall(r"end(\d+)\.", p) for p in text.split("\n\n")]


def test_breaks_mode_rebuilds_paragraphs_from_sentence_numbers() -> None:
    fake = FakeLlama(lambda chunk, n: "<think>\n\n</think>\n\n3, 5.")
    source = transcript(sentences(3), sentences(3, start=3))
    result = breaks(fake, paragraph_words=30).format(source, CTX)
    assert paragraphs_of(result.markdown) == [["0", "1"], ["2", "3"], ["4", "5"]]
    assert result.words_preserved and result.discrepancies == ()
    call = fake.calls[0]
    prompt = call["messages"][-1]["content"]
    assert "start a new paragraph" in prompt and "in en." in prompt
    assert "Choose about 1 numbers" in prompt
    assert chunk_of(call["messages"]).splitlines()[1] == "[2] " + sentences(1, start=1)
    assert call["temperature"] == 0.0 and call["max_tokens"] == 6 * 4 + 32
    assert "-breaks-" in breaks(fake).version


def test_breaks_unusable_reply_is_retried_then_left_unformatted() -> None:
    replies = iter(["Sure! Paragraphs start at 3", "99", "4"])
    fake = FakeLlama(lambda chunk, n: next(replies))
    result = breaks(fake, min_chunk_words=50, max_chunk_words=60, paragraph_words=30).format(
        transcript(sentences(6), sentences(6, start=6)), CTX
    )
    assert [(c["temperature"], c["seed"]) for c in fake.calls] == [(0.0, 0), (0.3, 1), (0.0, 0)]
    first = [str(i) for i in range(6)]
    assert paragraphs_of(result.markdown) == [first, ["6", "7", "8"], ["9", "10", "11"]]
    assert result.words_preserved
    assert result.discrepancies == (
        "chunk 1 of 2 (words 1-60): the model gave no usable breaks,"
        " so this part is left unformatted",
    )


def test_breaks_empty_reply_is_accepted_only_for_short_text() -> None:
    fake = FakeLlama(lambda chunk, n: "")
    short = breaks(fake).format(transcript(sentences(3)), CTX)
    assert short.markdown == sentences(3) and short.discrepancies == ()
    long = breaks(fake, min_chunk_words=20, max_chunk_words=40).format(
        transcript(sentences(3)), CTX
    )
    assert long.markdown == sentences(3) and len(long.discrepancies) == 1


def test_breaks_last_paragraph_is_carried_into_the_next_chunk() -> None:
    replies = iter(["11", "11, 21"])
    fake = FakeLlama(lambda chunk, n: next(replies))
    result = breaks(fake, min_chunk_words=100, max_chunk_words=200).format(
        transcript(sentences(20), sentences(20, start=20)), CTX
    )
    assert len(fake.calls) == 2
    second = chunk_of(fake.calls[1]["messages"]).splitlines()
    assert len(second) == 30 and second[0] == "[1] " + sentences(1, start=10)
    expected = [[str(i) for i in range(a, a + 10)] for a in (0, 10, 20, 30)]
    assert paragraphs_of(result.markdown) == expected
    assert result.words_preserved and result.discrepancies == ()


def test_breaks_single_sentence_needs_no_model() -> None:
    def factory(_: LlamaFormatterOptions) -> FakeLlama:
        raise AssertionError("model loaded")

    result = LlamaCppFormatter({}, model_factory=factory).format(transcript("Amen."), CTX)
    assert result.markdown == "Amen."


def test_breaks_too_short_paragraphs_are_merged() -> None:
    fake = FakeLlama(lambda chunk, n: ", ".join(str(i) for i in range(2, 11)))
    result = breaks(fake).format(transcript(sentences(10)), CTX)
    assert paragraphs_of(result.markdown) == [["0", "1", "2", "3"], [str(i) for i in range(4, 10)]]
