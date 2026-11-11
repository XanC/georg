# SPDX-License-Identifier: AGPL-3.0-only
"""Tiny .docx files for tests: each paragraph is a list of runs.

A run is a string; inside it "\t" becomes `w:tab` and "\n" becomes `w:br`.
"""

import zipfile
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import escape

_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

BULLETIN_1108: list[list[str]] = [
    ["Twenty-third Sunday after Trinity"],
    ["November 8, 2026"],
    ["Old Testament Reading", "Proverbs 8:11\u201322"],
    ["11", "For wisdom is better than jewels,", "and all that you may desire cannot compare."],
    ["Epistle", "Philippians 3:17\u201321"],
    ["17", "Brothers, join in imitating me, and keep your eyes on those who walk."],
    ["Holy Gospel", "Matthew 22:15\u201322"],
    ["15", "Then the Pharisees went and plotted how to entangle him in his words."],
]


def _run(text: str) -> str:
    parts = []
    for i, line in enumerate(text.split("\n")):
        if i:
            parts.append("<w:br/>")
        for j, piece in enumerate(line.split("\t")):
            if j:
                parts.append("<w:tab/>")
            if piece:
                parts.append(f'<w:t xml:space="preserve">{escape(piece)}</w:t>')
    return f"<w:r><w:rPr><w:b/></w:rPr>{''.join(parts)}</w:r>"


def make_docx(path: Path, paragraphs: Sequence[Sequence[str]]) -> Path:
    body = "".join(f"<w:p>{''.join(_run(r) for r in runs)}</w:p>" for runs in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_NS}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return path
