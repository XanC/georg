# SPDX-License-Identifier: AGPL-3.0-only
from datetime import date
from typing import Any

import pytest

from georg.core.contracts import MetadataGenerator
from georg.core.errors import ConfigError
from georg.core.types import MetadataContext, SermonMetadata, SourceItem, SourcePart
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
    assert RulesMetadataGenerator.version == "rules-2"
