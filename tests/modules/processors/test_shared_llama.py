# SPDX-License-Identifier: AGPL-3.0-only
from collections.abc import Iterator
from typing import Any

import pytest

from georg.core.types import FormatContext, SermonMetadata, Transcript, TranscriptSegment
from georg.modules.processors.format import LlamaCppFormatter
from georg.modules.processors.format.llama_cpp import LlamaModelOptions, release_shared_models
from georg.modules.processors.metadata import ScriptureDetector, ScriptureOptions

BULLETIN = "Holy GospelMatthew 22:15\u201322\n15Then the Pharisees went and plotted."
SERMON = "Whose likeness is this? Render to God the things that are God's."
CTX = FormatContext(language="en", metadata=SermonMetadata())


class FakeLlama:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def create_chat_completion(self, messages: Any, **kwargs: Any) -> Any:
        self.calls += 1
        reply = '{"readings": ["Gospel"]}' if "response_format" in kwargs else "Amen."
        return {"choices": [{"message": {"content": reply}}]}

    def close(self) -> None:
        self.closed = True


class Loader:
    def __init__(self) -> None:
        self.models: list[tuple[int, FakeLlama]] = []

    def __call__(self, options: LlamaModelOptions) -> FakeLlama:
        model = FakeLlama()
        self.models.append((options.context_size, model))
        return model


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    release_shared_models()
    yield
    release_shared_models()


def transcript(text: str) -> Transcript:
    segment = TranscriptSegment.model_validate(
        {"range": {"start_ms": 0, "end_ms": 1000}, "text": text}
    )
    return Transcript(language="en", segments=(segment,), provider="test")


def test_formatter_and_scripture_detector_share_one_load() -> None:
    load = Loader()
    formatter = LlamaCppFormatter({"mode": "rewrite"}, model_factory=load)
    detector = ScriptureDetector(ScriptureOptions(), model_factory=load)
    found = detector.detect(SERMON, {"bulletin.docx": BULLETIN})
    assert found is not None and found.references == ("Matthew 22:15-22",)
    assert formatter.format(transcript("Amen."), CTX).markdown == "Amen."
    [(context, model)] = load.models
    assert context == 8192  # the larger of the formatter's 4096 and the detector's 8192
    assert model.calls == 2


def test_larger_context_reserved_later_replaces_the_shared_model() -> None:
    load = Loader()
    formatter = LlamaCppFormatter({"mode": "rewrite"}, model_factory=load)
    formatter.format(transcript("Amen."), CTX)
    detector = ScriptureDetector(ScriptureOptions(), model_factory=load)
    detector.detect(SERMON, {"bulletin.docx": BULLETIN})
    formatter.format(transcript("Amen."), CTX)
    (small, first), (large, second) = load.models
    assert (small, large) == (4096, 8192)
    assert first.closed and first.calls == 1
    assert second.calls == 2 and not second.closed


def test_different_model_files_are_loaded_separately() -> None:
    load = Loader()
    LlamaCppFormatter({"mode": "rewrite"}, model_factory=load).format(transcript("Amen."), CTX)
    other = LlamaCppFormatter({"mode": "rewrite", "model_file": "other.gguf"}, model_factory=load)
    other.format(transcript("Amen."), CTX)
    assert len(load.models) == 2
