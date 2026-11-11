# SPDX-License-Identifier: AGPL-3.0-only
import logging
import zipfile
from pathlib import Path

import pytest
from docx_fixture import BULLETIN_1108, make_docx

from georg.documents import docx_text, extract_text, is_supported


def test_docx_paragraphs_runs_tabs_and_breaks(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "a.docx", [["Holy Gospel", "Matthew 22:15\u201322"],
                                           ["a\tb", "c\nd"], []])  # fmt: skip
    assert docx_text(path) == "Holy GospelMatthew 22:15\u201322\na\tbc\nd\n"


def test_bulletin_1108_text(tmp_path: Path) -> None:
    text = docx_text(make_docx(tmp_path / "b.docx", BULLETIN_1108))
    lines = text.splitlines()
    assert lines[:3] == [
        "Twenty-third Sunday after Trinity",
        "November 8, 2026",
        "Old Testament ReadingProverbs 8:11\u201322",
    ]
    assert lines[3].startswith("11For wisdom is better than jewels,and all")


def test_extract_text_skips_other_formats_and_bad_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    pdf = tmp_path / "bulletin.pdf"
    pdf.write_bytes(b"%PDF")
    assert extract_text(pdf) is None
    assert "only .docx and .txt" in caplog.text
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip")
    assert extract_text(broken) is None
    empty = tmp_path / "empty.docx"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("other.xml", "<x/>")
    assert extract_text(empty) is None
    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")
    assert extract_text(notes) == "hello"
    assert is_supported("A.DOCX") and not is_supported("a.doc")
