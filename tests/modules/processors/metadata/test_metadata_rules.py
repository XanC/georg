# SPDX-License-Identifier: AGPL-3.0-only
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from docx_fixture import BULLETIN_1108, make_docx

from georg.core.contracts import ExplainingMetadataGenerator, MetadataGenerator
from georg.core.errors import ConfigError
from georg.core.types import (
    MetadataContext,
    SermonMetadata,
    SourceItem,
    SourcePart,
    Transcript,
    TranscriptSegment,
)
from georg.documents import docx_text
from georg.modules.processors.format.llama_cpp import LlamaModelOptions
from georg.modules.processors.metadata import (
    DocumentHint,
    RulesMetadataGenerator,
    parse_document_name,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("2026-11-01_All_Saints__Day_-_DS1-Eric.docx",
         DocumentHint(date(2026, 11, 1), "All Saints' Day", "DS1", "Eric")),
        ("2026-11-08_Twenty-third_Sunday_after_Trinity_-_DS1-Eric.docx",
         DocumentHint(date(2026, 11, 8), "Twenty-third Sunday after Trinity", "DS1", "Eric")),
        ("2026-11-22_Christ_the_King__-_DS2.docx",
         DocumentHint(date(2026, 11, 22), "Christ the King", "DS2", None)),
        ("2026-11-08_Pentecost_24.docx",
         DocumentHint(date(2026, 11, 8), "Pentecost 24", None, None)),
        ("2026-12-24_Christmas_Eve_-_DS2.docx",
         DocumentHint(date(2026, 12, 24), "Christmas Eve", "DS2", None)),
        ("2026-12-27_Christmas_1_-_Sarah.DOCX",
         DocumentHint(date(2026, 12, 27), "Christmas 1", None, "Sarah")),
        ("2027-01-03_Epiphany_-_DS3-Mary-Ann_Smith.docx",
         DocumentHint(date(2027, 1, 3), "Epiphany", "DS3", "Mary-Ann Smith")),
        ("2026-11-01 St. Paul Day - DS1-Eric.pdf",
         DocumentHint(date(2026, 11, 1), "St. Paul Day", "DS1", "Eric")),
        ("folder/2026-11-01_Reformation.docx",
         DocumentHint(date(2026, 11, 1), "Reformation", None, None)),
        ("2026-11-01.docx", DocumentHint(date(2026, 11, 1), None, None, None)),
    ],
)  # fmt: skip
def test_parse_document_name(name: str, expected: DocumentHint) -> None:
    assert parse_document_name(name) == expected


@pytest.mark.parametrize(
    "name", ["Bulletin.docx", "2026-13-01_Bad_Month.docx", "11.01.26_All_Saints.docx", ""]
)
def test_unparseable_names(name: str) -> None:
    assert parse_document_name(name) is None


DEFAULTS = SermonMetadata(preacher="Pastor Default", service_time="10:30", extra={"k": 1})


def ctx(service_date: date | None, documents: Any = None) -> MetadataContext:
    hints = {} if documents is None else {"documents": documents}
    source = SourceItem(
        source_type="google_drive",
        source_ref="folder-1",
        parts=(SourcePart(ref="f1", name="service.mp4"),),
        service_date=service_date,
        hints=hints,
    )
    return MetadataContext(source=source, transcript=None, defaults=DEFAULTS)


def test_generate_from_document_and_defaults() -> None:
    gen = RulesMetadataGenerator()
    assert isinstance(gen, MetadataGenerator)
    meta = gen.generate(ctx(date(2026, 11, 1), ["2026-11-01_All_Saints__Day_-_DS1-Eric.docx"]))
    assert meta.service_date == date(2026, 11, 1)
    assert meta.occasion == "All Saints' Day"
    assert meta.preacher == "Eric"
    assert meta.service_time == "10:30"
    assert meta.extra == {"k": 1}
    assert meta.title is None and meta.scripture == () and meta.summary is None


def test_preacher_aliases_and_mapping_documents() -> None:
    gen = RulesMetadataGenerator({"preachers": {"eric": "The Rev. Eric Example"}})
    docs = [{"name": "notes.txt"}, {"name": "2026-11-01_All_Saints__Day_-_DS1-Eric.docx"}]
    assert gen.generate(ctx(date(2026, 11, 1), docs)).preacher == "The Rev. Eric Example"


def test_document_for_another_date_is_ignored() -> None:
    meta = RulesMetadataGenerator().generate(
        ctx(date(2026, 11, 8), "2026-11-01_All_Saints__Day_-_DS1-Eric.docx")
    )
    assert meta.service_date == date(2026, 11, 8)
    assert meta.occasion is None and meta.preacher == "Pastor Default"


def test_document_date_used_when_source_has_none() -> None:
    meta = RulesMetadataGenerator().generate(ctx(None, ["2026-11-08_Pentecost_24.docx"]))
    assert meta.service_date == date(2026, 11, 8)
    assert meta.occasion == "Pentecost 24" and meta.preacher == "Pastor Default"


def test_no_hints_returns_defaults() -> None:
    meta = RulesMetadataGenerator().generate(ctx(None))
    assert meta == DEFAULTS


def test_bad_options() -> None:
    with pytest.raises(ConfigError):
        RulesMetadataGenerator({"preacher_map": {}})


def test_preacher_from_documents_disabled() -> None:
    gen = RulesMetadataGenerator({"preacher_from_documents": False})
    doc = "2026-11-01_All_Saints__Day_-_DS1-Eric.docx"
    meta = gen.generate(ctx(date(2026, 11, 1), [doc]))
    assert meta.preacher == "Pastor Default" and meta.occasion == "All Saints' Day"
    source = ctx(date(2026, 11, 1), [doc]).source
    bare = MetadataContext(source=source, transcript=None, defaults=SermonMetadata())
    assert gen.generate(bare).preacher is None


def test_service_time_default_fills_only_missing_time() -> None:
    gen = RulesMetadataGenerator({"service_time": "09:15"})
    source = ctx(date(2026, 11, 1)).source
    empty = MetadataContext(source=source, transcript=None, defaults=SermonMetadata())
    assert gen.generate(empty).service_time == "09:15"
    assert gen.generate(ctx(date(2026, 11, 1))).service_time == "10:30"


@pytest.mark.parametrize("value", ["9:15", "24:00", "10:60", "10:30:00", "ten", ""])
def test_invalid_service_time_rejected(value: str) -> None:
    with pytest.raises(ConfigError, match="service_time"):
        RulesMetadataGenerator({"service_time": value})


def test_title_template_with_occasion() -> None:
    gen = RulesMetadataGenerator(
        {"title": "Sermon for {occasion}", "preachers": {"eric": "Pastor Eric"}}
    )
    doc = "2026-11-01_All_Saints__Day_-_DS1-Eric.docx"
    assert gen.generate(ctx(date(2026, 11, 1), [doc])).title == "Sermon for All Saints' Day"
    gen = RulesMetadataGenerator({"title": "{date}: {occasion} ({preacher})"})
    assert gen.generate(ctx(date(2026, 11, 1), [doc])).title == (
        "2026-11-01: All Saints' Day (Eric)"
    )


@pytest.mark.parametrize(
    ("doc", "occasion", "title"),
    [
        ("2026-05-31_The_Holy_Trinity.docx", "The Holy Trinity", "Sermon for the Holy Trinity"),
        ("2026-05-24_The_Day_of_Pentecost_-_DS1.docx", "The Day of Pentecost",
         "Sermon for the Day of Pentecost"),
        ("2026-09-20_Fifteenth_Sunday_after_Trinity.docx", "Fifteenth Sunday after Trinity",
         "Sermon for Fifteenth Sunday after Trinity"),
        ("2026-05-14_Theophany.docx", "Theophany", "Sermon for Theophany"),
    ],
)  # fmt: skip
def test_title_lowercases_leading_the(doc: str, occasion: str, title: str) -> None:
    gen = RulesMetadataGenerator({"title": "Sermon for {occasion}"})
    meta = gen.generate(ctx(None, [doc]))
    assert meta.occasion == occasion and meta.title == title


def test_title_stays_empty_without_occasion() -> None:
    gen = RulesMetadataGenerator({"title": "Sermon for {occasion}"})
    meta = gen.generate(ctx(date(2026, 11, 1), ["Bulletin.docx"]))
    assert meta.title is None and meta.occasion is None


def test_existing_title_kept() -> None:
    gen = RulesMetadataGenerator({"title": "Sermon for {occasion}"})
    source = ctx(date(2026, 11, 1), ["2026-11-01_All_Saints__Day_-_DS1-Eric.docx"]).source
    defaults = SermonMetadata(title="Blessed Are")
    meta = gen.generate(MetadataContext(source=source, transcript=None, defaults=defaults))
    assert meta.title == "Blessed Are" and meta.occasion == "All Saints' Day"


@pytest.mark.parametrize(
    "template", ["Sermon for {title}", "{occasion!r}", "{occasion:>9}", "{}", "{occasion"]
)
def test_invalid_title_template_rejected(template: str) -> None:
    with pytest.raises(ConfigError, match="title"):
        RulesMetadataGenerator({"title": template})


def test_version_bumped_for_new_options() -> None:
    assert RulesMetadataGenerator.version == "rules-3"


DOC_1108 = "2026-11-08_Twenty-third_Sunday_after_Trinity_-_DS1-Eric.docx"
DOC_1101 = "2026-11-01_All_Saints__Day_-_DS1-Eric.docx"


def doc_ctx(
    service_date: date | None,
    texts: dict[str, str],
    names: list[str] | None = None,
    transcript: str = "",
    defaults: SermonMetadata | None = None,
) -> MetadataContext:
    base = ctx(service_date, names if names is not None else list(texts))
    sermon = None
    if transcript:
        segment = TranscriptSegment.model_validate(
            {"range": {"start_ms": 0, "end_ms": 1000}, "text": transcript}
        )
        sermon = Transcript(language="en", segments=(segment,), provider="test")
    return base.model_copy(
        update={"documents": texts, "transcript": sermon, "defaults": defaults or DEFAULTS}
    )


def bulletin(tmp_path: Path, paragraphs: list[list[str]] = BULLETIN_1108) -> str:
    return docx_text(make_docx(tmp_path / "b.docx", paragraphs))


def test_occasion_from_bulletin_text_1108(tmp_path: Path) -> None:
    gen = RulesMetadataGenerator({"title": "Sermon for {occasion}"})
    assert isinstance(gen, ExplainingMetadataGenerator) and gen.uses_documents
    proposal = gen.propose(doc_ctx(date(2026, 11, 8), {DOC_1108: bulletin(tmp_path)}))
    assert proposal.metadata.occasion == "Twenty-third Sunday after Trinity"
    assert proposal.metadata.title == "Sermon for Twenty-third Sunday after Trinity"
    assert proposal.origins == {"occasion": f"rules-3/document text: {DOC_1108}"}


def test_text_occasion_preferred_over_file_name(tmp_path: Path) -> None:
    text = bulletin(tmp_path, [["All Saints\u2019  Day"], ["Nov. 1, 2026"], ["Welcome"]])
    gen = RulesMetadataGenerator({"title": "Sermon for {occasion}"})
    proposal = gen.propose(doc_ctx(date(2026, 11, 1), {DOC_1101: text}))
    assert proposal.metadata.occasion == "All Saints' Day"
    assert proposal.metadata.title == "Sermon for All Saints' Day"
    text = bulletin(tmp_path, [["The Feast of All Saints"], ["1 November 2026"]])
    meta = gen.generate(doc_ctx(date(2026, 11, 1), {DOC_1101: text}))
    assert meta.occasion == "The Feast of All Saints"
    assert meta.title == "Sermon for the Feast of All Saints"


def test_text_occasion_with_other_date_falls_back_to_file_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = bulletin(tmp_path, [["Reformation Day"], ["October 25, 2026"]])
    proposal = RulesMetadataGenerator().propose(doc_ctx(date(2026, 11, 1), {DOC_1101: text}))
    assert proposal.metadata.occasion == "All Saints' Day"
    assert proposal.origins == {"occasion": f"rules-3/document name: {DOC_1101}"}
    assert "dated 2026-10-25, not 2026-11-01" in caplog.text


@pytest.mark.parametrize("texts", [{}, {DOC_1101: ""}, {DOC_1101: "Bulletin\nWelcome to St. Paul"}])
def test_unreadable_or_unmatched_document_falls_back(texts: dict[str, str]) -> None:
    proposal = RulesMetadataGenerator().propose(doc_ctx(date(2026, 11, 1), texts, names=[DOC_1101]))
    assert proposal.metadata.occasion == "All Saints' Day"
    assert proposal.origins["occasion"].startswith("rules-3/document name:")


def test_occasion_from_text_can_be_disabled(tmp_path: Path) -> None:
    gen = RulesMetadataGenerator({"occasion_from_document_text": False})
    assert not gen.uses_documents
    meta = gen.generate(doc_ctx(date(2026, 11, 8), {DOC_1108: bulletin(tmp_path)}))
    assert meta.occasion == "Twenty-third Sunday after Trinity"  # from the file name
    other = gen.generate(doc_ctx(date(2026, 11, 1), {DOC_1101: "Feast\nNovember 1, 2026"}))
    assert other.occasion == "All Saints' Day"


class FakeLlama:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def create_chat_completion(self, messages: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return {"choices": [{"message": {"content": self.reply}}]}


SERMON = (
    "Did you hear in the text how they butter them up? Whose likeness is this? St. Paul "
    "says in his letter to the Philippians that our citizenship is in heaven. In Genesis "
    "chapter 1 God made man in his image."
)


def scripture_gen(fake: FakeLlama, **options: Any) -> RulesMetadataGenerator:
    def factory(_: LlamaModelOptions) -> FakeLlama:
        return fake

    return RulesMetadataGenerator({"scripture": {}, **options}, model_factory=factory)


def test_scripture_detected_with_origin(tmp_path: Path) -> None:
    fake = FakeLlama('{"readings": ["Gospel", "Epistle", "Genesis"]}')
    gen = scripture_gen(fake, title="Sermon for {occasion}", preacher_from_documents=False)
    assert gen.version.startswith("rules-3+scripture-2-")
    proposal = gen.propose(
        doc_ctx(date(2026, 11, 8), {DOC_1108: bulletin(tmp_path)}, transcript=SERMON)
    )
    meta = proposal.metadata
    assert meta.scripture == ("Matthew 22:15-22", "Philippians 3:17-21")
    assert meta.preacher == "Pastor Default"
    assert proposal.origins["scripture"].startswith("scripture-2/llm: local model picked")
    assert proposal.origins["occasion"] == f"rules-3/document text: {DOC_1108}"


def test_scripture_never_overrides_and_never_fails(tmp_path: Path) -> None:
    fake = FakeLlama('{"readings": ["Gospel"]}')
    gen = scripture_gen(fake)
    preset = DEFAULTS.model_copy(update={"scripture": ("John 3:16",)})
    texts = {DOC_1108: bulletin(tmp_path)}
    proposal = gen.propose(doc_ctx(date(2026, 11, 8), texts, transcript=SERMON, defaults=preset))
    assert proposal.metadata.scripture == ("John 3:16",) and "scripture" not in proposal.origins
    assert fake.calls == 0
    empty = gen.propose(doc_ctx(date(2026, 11, 8), texts))
    assert empty.metadata.scripture == () and "scripture" not in empty.origins

    def broken(_: LlamaModelOptions) -> FakeLlama:
        raise RuntimeError("out of memory")

    failing = RulesMetadataGenerator({"scripture": {}}, model_factory=broken)
    meta = failing.generate(doc_ctx(date(2026, 11, 8), texts, transcript=SERMON))
    assert meta.scripture == () and meta.occasion == "Twenty-third Sunday after Trinity"
