# SPDX-License-Identifier: AGPL-3.0-only
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from docx_fixture import BULLETIN_1108, make_docx

from georg.documents import docx_text
from georg.modules.processors.format.llama_cpp import LlamaModelOptions
from georg.modules.processors.metadata import (
    ScriptureDetector,
    ScriptureOptions,
    normalize_reference,
    parse_header,
    parse_readings,
    parse_reference,
)
from georg.modules.processors.metadata.readings import parse_date_line
from georg.modules.processors.metadata.references import find_references

SERMON_1108 = (
    "Grace to you and peace. Did you hear in the text how they butter them up? Teacher, "
    "we know that you are true. They want to trap him with a coin. Whose likeness is "
    "this? Caesar's. This is what St. Paul says in his letter to the Philippians, which "
    "you heard just a few minutes ago: our citizenship is in heaven. In Genesis chapter "
    "1 God made man in his image. So render to God the things that are God's. Amen."
)


@pytest.fixture
def bulletin(tmp_path: Path) -> str:
    return docx_text(make_docx(tmp_path / "bulletin.docx", BULLETIN_1108))


class FakeLlama:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        return {"choices": [{"message": {"content": self.reply}}]}


def detector(fake: FakeLlama | None = None, **options: Any) -> ScriptureDetector:
    def factory(_: LlamaModelOptions) -> FakeLlama:
        if fake is None:
            raise AssertionError("the model should not be used")
        return fake

    return ScriptureDetector(ScriptureOptions.model_validate(options), model_factory=factory)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ephesians 6:1-4", "Ephesians 6:1-4"),
        ("Matthew 22:15\u201322", "Matthew 22:15-22"),
        ("Matthew 13:24\u201430 , 36 \u2212 43", "Matthew 13:24-30, 36-43"),
        ("John 18:1\u201119:42", "John 18:1-19:42"),
        ("Romans 7:14\u201225a", "Romans 7:14-25a"),
        ("1 Corinthians 13", "1 Corinthians 13"),
        ("Psalm 23", "Psalm 23"),
        ("Song of Songs 2:8-13", "Song of Solomon 2:8-13"),
        ("Ephesians chapter 6, verses 1 through 4", "Ephesians 6:1-4"),
        ("Ephesians six, one through four", "Ephesians 6:1-4"),
        ("Ephesians chapter six, verse one", "Ephesians 6:1"),
        ("First Corinthians chapter thirteen", "1 Corinthians 13"),
        ("Second Peter one, sixteen to twenty-one", "2 Peter 1:16-21"),
        ("Matthew chapter twenty two, beginning at verse fifteen", "Matthew 22:15"),
        ("Psalm one hundred and nineteen, verses one hundred five through one hundred twelve",
         "Psalm 119:105-112"),
        ("John chapter 18 verse 1 through chapter 19 verse 42", "John 18:1-19:42"),
    ],
)  # fmt: skip
def test_written_and_spoken_references(text: str, expected: str) -> None:
    parsed = parse_reference(text)
    assert parsed is not None and parsed[0] == expected


@pytest.mark.parametrize("text", ["Isaiah", "the book of Isaiah", "Mark my words", "Jude 0x"])
def test_not_references(text: str) -> None:
    assert parse_reference(text) is None


def test_find_references_and_normalize() -> None:
    found = find_references("in Genesis chapter 1 and Philippians 3:17\u201321 we read")
    assert [r for r, _, _ in found] == ["Genesis 1", "Philippians 3:17-21"]
    assert normalize_reference("Luke 17 : 11 \u2013 19") == "Luke 17:11-19"


def test_bulletin_readings(bulletin: str) -> None:
    readings = parse_readings(bulletin)
    assert [(r.label, r.heading, r.reference) for r in readings] == [
        ("Old Testament", "Old Testament Reading", "Proverbs 8:11-22"),
        ("Epistle", "Epistle", "Philippians 3:17-21"),
        ("Gospel", "Holy Gospel", "Matthew 22:15-22"),
    ]
    assert (
        readings[2].text == "Then the Pharisees went and plotted how to entangle him in his words."
    )
    assert readings[0].text.startswith("For wisdom is better than jewels,and all")


def test_readings_custom_headings_psalm_and_excerpt() -> None:
    text = "First Lesson: Isaiah 55:1\u20135\n1Come, everyone\nPsalm 23\n1The Lord\nGospel Luke 2:1"
    readings = parse_readings(text, {"First Lesson": "First", "Psalm": "Psalm", "Gospel": "Gospel"},
                              excerpt_chars=5)  # fmt: skip
    assert [(r.label, r.reference, r.text) for r in readings] == [
        ("First", "Isaiah 55:1-5", "Come,"),
        ("Psalm", "Psalm 23", "The L"),
        ("Gospel", "Luke 2:1", ""),
    ]


def test_bulletin_header(bulletin: str) -> None:
    header = parse_header(bulletin)
    assert header is not None
    assert header.occasion == "Twenty-third Sunday after Trinity"
    assert header.service_date == date(2026, 11, 8)
    curly = parse_header("  All Saints\u2019   Day \n\n8 November 2026\n")
    assert curly is not None and curly.occasion == "All Saints' Day"
    assert parse_header("Holy GospelMatthew 22:15\nNovember 8, 2026") is None
    assert parse_header("Welcome\nPlease silence your phones") is None
    assert parse_header("November 8, 2026") is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("November 8, 2026", date(2026, 11, 8)),
        ("Nov. 8, 2026", date(2026, 11, 8)),
        ("8 November 2026", date(2026, 11, 8)),
        ("Sunday, November 8, 2026", date(2026, 11, 8)),
        ("Sept. 6, 2026", date(2026, 9, 6)),
        ("February 30, 2026", None),
        ("Novembr 8, 2026", None),
        ("Trinity 23, 2026", None),
    ],
)
def test_date_lines(line: str, expected: date | None) -> None:
    assert parse_date_line(line) == expected


@pytest.mark.parametrize(
    "sermon",
    [
        "Grace to you. The text for this morning is Ephesians 6:1\u20134. Children, obey.",
        "Our text this morning comes from Ephesians chapter six, verses one through four.",
        "Today's text is Ephesians six, one through four, where Paul writes to children.",
        "The sermon text for today is taken from Ephesians 6:1-4.",
    ],
)
def test_method_1_stated_text(sermon: str) -> None:
    found = detector().detect(sermon, {})
    assert found is not None
    assert found.references == ("Ephesians 6:1-4",)
    assert found.method == "text"
    assert found.origin.startswith('scripture-2/text: sermon says "')


@pytest.mark.parametrize(
    ("sermon", "expected"),
    [
        ("The text for this morning comes from the Gospel reading, where they test him.",
         "Matthew 22:15-22"),
        ("The text for our meditation is taken from our reading of the book of Proverbs.",
         "Proverbs 8:11-22"),
        ("Our text is our Epistle, which you heard a few minutes ago.", "Philippians 3:17-21"),
        ("Our text this morning is the Old Testament reading.", "Proverbs 8:11-22"),
    ],
)  # fmt: skip
def test_method_2_named_reading(bulletin: str, sermon: str, expected: str) -> None:
    found = detector().detect(sermon, {"bulletin.docx": bulletin})
    assert found is not None and found.method == "reading"
    assert found.references == (expected,)
    assert "bulletin" in found.reason and expected in found.reason


@pytest.mark.parametrize(
    "sermon",
    [
        SERMON_1108,
        "In the Gospel reading Jesus is tested. The text is clear about that.",
        "As Philippians 3:20 says, our citizenship is in heaven. Read the text at home.",
        "Our text for today is short.",
    ],
)
def test_incidental_mentions_are_not_methods_1_or_2(bulletin: str, sermon: str) -> None:
    assert detector(llm_enabled=False).detect(sermon, {"b.docx": bulletin}) is None


def test_method_3_scenario_1108(bulletin: str, tmp_path: Path) -> None:
    fake = FakeLlama('{"readings": ["Gospel", "Epistle", "Genesis", "Gospel", 3]}')
    found = detector(fake, results_dir=tmp_path / "picks").detect(
        SERMON_1108, {"2026-11-08_Twenty-third_Sunday_after_Trinity_-_DS1-Eric.docx": bulletin}
    )
    assert found is not None
    assert found.references == ("Matthew 22:15-22", "Philippians 3:17-21")
    assert found.method == "llm"
    assert found.origin.startswith("scripture-2/llm: local model picked Gospel, Epistle")
    [call] = fake.calls
    prompt = call["messages"][-1]["content"]
    assert call["messages"][0]["content"] == "/no_think"
    assert "[Gospel] Matthew 22:15-22\nThen the Pharisees" in prompt
    assert "Whose likeness is this?" in prompt
    schema = call["response_format"]["schema"]
    assert schema["properties"]["readings"]["items"]["enum"] == [
        "Old Testament",
        "Epistle",
        "Gospel",
    ]
    assert call["temperature"] == 0.0
    # Cached by model version, transcript and readings: a second detector reuses the pick.
    again = detector(None, results_dir=tmp_path / "picks").detect(SERMON_1108, {"b": bulletin})
    assert again == found
    assert len(list((tmp_path / "picks").glob("*.json"))) == 1


@pytest.mark.parametrize(
    "reply", ['{"readings": []}', '{"readings": ["Hebrews"]}', "I think the Gospel.", "[]"]
)
def test_method_3_nothing_usable(bulletin: str, reply: str) -> None:
    assert detector(FakeLlama(reply)).detect(SERMON_1108, {"b": bulletin}) is None


def test_method_3_reply_with_fence_and_bare_list(bulletin: str) -> None:
    fake = FakeLlama('```json\n["epistle"]\n```')
    found = detector(fake).detect(SERMON_1108, {"b": bulletin})
    assert found is not None and found.references == ("Philippians 3:17-21",)


def test_failures_leave_scripture_empty(bulletin: str, caplog: pytest.LogCaptureFixture) -> None:
    def broken(_: LlamaModelOptions) -> FakeLlama:
        raise RuntimeError("no model")

    failing = ScriptureDetector(ScriptureOptions(), model_factory=broken)
    assert failing.detect(SERMON_1108, {"b": bulletin}) is None
    assert "the model failed: no model" in caplog.text
    assert detector().detect("   ", {"b": bulletin}) is None
    assert detector().detect(SERMON_1108, {}) is None
    assert detector().detect(SERMON_1108, {"notes.txt": "nothing here"}) is None


def test_long_transcripts_are_cut(bulletin: str) -> None:
    fake = FakeLlama(json.dumps({"readings": ["Gospel"]}))
    words = " ".join(f"w{i}" for i in range(300))
    detector(fake, llm={"max_transcript_words": 100, "context_size": 2048}).detect(
        words, {"b": bulletin}
    )
    prompt = fake.calls[0]["messages"][-1]["content"]
    assert "w99 [...]" in prompt and "w100" not in prompt


def test_version_tracks_model_and_prompt() -> None:
    assert detector().version.startswith("scripture-2-Qwen3-4B-Instruct-2507-Q4_K_M.gguf-prompt-v1")
    assert detector(llm_enabled=False).version == "scripture-2-nollm"
    with pytest.raises(ValueError, match="context_size"):
        ScriptureOptions.model_validate({"llm": {"context_size": 2048}})


@pytest.mark.parametrize(
    ("bulletin_text", "label", "reference"),
    [
        ("First ReadingActs 2:1\u201321\n1When the day of Pentecost arrived", "First Reading",
         "Acts 2:1-21"),
        ("First ReadingIsaiah 55:1\u20135\n1Come, everyone who thirsts", "Old Testament",
         "Isaiah 55:1-5"),
    ],
)  # fmt: skip
def test_first_reading_is_labelled_by_its_book(
    bulletin_text: str, label: str, reference: str
) -> None:
    [reading] = parse_readings(bulletin_text)
    assert (reading.label, reading.reference) == (label, reference)
    for sermon in ("Our text this morning is the first reading.",
                   "The text for today comes from our first lesson."):  # fmt: skip
        found = detector().detect(sermon, {"b.docx": bulletin_text})
        assert found is not None and found.references == (reference,)
