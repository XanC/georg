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
         DocumentHint(date(2026, 11, 1), "All Saints Day", "DS1", "Eric")),
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
    assert meta.occasion == "All Saints Day"
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
