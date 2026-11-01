# SPDX-License-Identifier: AGPL-3.0-only
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from georg.core.contracts import TranscriptFormatter
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import (
    FormatContext,
    SermonMetadata,
    TimeRange,
    Transcript,
    TranscriptSegment,
)
from georg.modules.processors.format import OpenAIChatFormatter, split_for_formatting

CTX = FormatContext(language="en", metadata=SermonMetadata())


def transcript(*texts: str) -> Transcript:
    segments = tuple(
        TranscriptSegment(text=t, range=TimeRange(start_ms=i * 1000, end_ms=(i + 1) * 1000))
        for i, t in enumerate(texts)
    )
    return Transcript(language="en", segments=segments, provider="test")


def chat_reply(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content},
                           "finish_reason": "stop"}]},
    )  # fmt: skip


def prompt_text(request: httpx.Request) -> str:
    body = json.loads(request.read())
    content: str = body["messages"][0]["content"]
    return content.split("Text:\n", 1)[1].strip()


def make(handler: Callable[[httpx.Request], httpx.Response], **options: Any) -> OpenAIChatFormatter:
    return OpenAIChatFormatter(
        {"api_key": "test-key", **options},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )


def test_formats_and_checks_words() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return chat_reply("**Sermon Transcript**\n\nGrace to you.\n\nAnd peace!")

    formatter = make(handler)
    assert isinstance(formatter, TranscriptFormatter)
    result = formatter.format(transcript("grace to you", "and peace"), CTX)
    assert result.markdown == "Grace to you.\n\nAnd peace!"
    assert result.words_preserved and result.discrepancies == ()
    body = json.loads(seen[0].read())
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert body["model"] == "gpt-4o-mini" and body["temperature"] == 0.0
    prompt = body["messages"][0]["content"]
    assert "Do NOT change any words" in prompt and "(en)" in prompt
    assert prompt_text(seen[0]) == "grace to you and peace"
    assert formatter.version == "openai_chat-1+prompt-v1"


def test_heading_matching_the_transcript_is_kept() -> None:
    result = make(lambda _: chat_reply("## Grace to you\nand peace.")).format(
        transcript("grace to you and peace"), CTX
    )
    assert result.words_preserved
    assert result.markdown.startswith("## Grace")


def test_mismatch_is_retried_once_then_succeeds() -> None:
    replies = iter(["Grace to all of you.", "Grace to you."])
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return chat_reply(next(replies))

    result = make(handler).format(transcript("grace to you"), CTX)
    assert len(calls) == 2
    assert result.words_preserved and result.markdown == "Grace to you."


def test_persistent_mismatch_is_flagged_not_raised() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return chat_reply("```\nGrace to all of you.\n```")

    result = make(handler).format(transcript("grace to you"), CTX)
    assert len(calls) == 2
    assert not result.words_preserved
    assert result.markdown == "Grace to all of you."
    assert result.discrepancies == ('word 3: inserted "all of"',)


def test_long_transcript_is_chunked_by_segment() -> None:
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        text = prompt_text(request)
        prompts.append(text)
        return chat_reply(text.capitalize() + ".")

    segs = ("one two three", "four five", "six seven eight nine", "ten")
    result = make(handler, max_chunk_words=5).format(transcript(*segs), CTX)
    assert prompts == ["one two three four five", "six seven eight nine ten"]
    assert result.markdown == "One two three four five.\n\nSix seven eight nine ten."
    assert result.words_preserved


def test_split_breaks_oversized_segments() -> None:
    chunks = split_for_formatting(transcript("a b c d e f g", "h"), 3)
    assert chunks == ["a b c", "d e f", "g h"]


def test_empty_transcript_makes_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    result = make(handler).format(transcript(), CTX)
    assert result.markdown == "" and result.words_preserved


def test_http_failure_raises_module_error() -> None:
    with pytest.raises(ModuleError) as info:
        make(lambda _: httpx.Response(500), attempts=1).format(transcript("x"), CTX)
    assert info.value.retryable


def test_custom_prompt_file_and_bad_options(tmp_path: Path) -> None:
    custom = tmp_path / "p.txt"
    custom.write_text("Format ($language):\nText:\n$text\n")
    formatter = make(lambda r: chat_reply(prompt_text(r)), prompt_file=str(custom))
    assert formatter.version.startswith("openai_chat-1+prompt-file-")
    assert formatter.format(transcript("amen"), CTX).words_preserved
    with pytest.raises(ConfigError, match="not found"):
        make(lambda _: chat_reply(""), prompt_version="v999")
    missing_text = tmp_path / "bad.txt"
    missing_text.write_text("no placeholder")
    with pytest.raises(ConfigError, match=r"\$text"):
        make(lambda _: chat_reply(""), prompt_file=str(missing_text))
