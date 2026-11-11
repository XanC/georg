# SPDX-License-Identifier: AGPL-3.0-only
"""The day's Scripture readings listed in a service bulletin.

A reading starts on a line beginning with a known heading followed by a reference, as
in "Holy GospelMatthew 22:15\u201322" (heading and reference are separate runs of one
paragraph, so they arrive glued together). The lines after it, up to the next reading,
are its text; verse numbers glued to words ("15Then the Pharisees") are dropped.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from georg.modules.processors.metadata.references import canonical_book, parse_reference

DEFAULT_HEADINGS: dict[str, str] = {
    "Old Testament Reading": "Old Testament",
    "Old Testament Lesson": "Old Testament",
    "First Reading": "Old Testament",
    "Psalm": "Psalm",
    "Epistle": "Epistle",
    "Second Reading": "Epistle",
    "Holy Gospel": "Gospel",
    "Gospel": "Gospel",
}
"""Bulletin heading -> reading label (what the sermon and the review page call it)."""

_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "Old Testament": ("old testament", "first reading", "first lesson"),
    "Epistle": ("epistle", "second reading", "second lesson"),
    "Gospel": ("gospel", "holy gospel"),
    "Psalm": ("psalm",),
}
_ORDINAL_WORDS = frozenset({"first", "second", "third"})
_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
_DATE_LINE = re.compile(
    r"(?:[A-Za-z]+,?\s+)?(?:(?P<m1>[A-Za-z]{3,})\.?\s+(?P<d1>\d{1,2}),?\s+(?P<y1>\d{4})"
    r"|(?P<d2>\d{1,2})\s+(?P<m2>[A-Za-z]{3,})\.?,?\s+(?P<y2>\d{4}))"
)
_VERSE_NUMBER = re.compile(r"(?<![\w:])\d{1,3}(?=[^\W\d_]|[\"'\u201c\u2018(\[])")
_STRAIGHT = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"'})
_AFTER_HEADING = re.compile(r"[\s:.\u2013\u2014-]*")


@dataclass(frozen=True)
class Reading:
    label: str
    heading: str
    reference: str
    text: str
    """The start of the reading's text, without verse numbers."""

    @property
    def book(self) -> str:
        return self.reference.rsplit(" ", 1)[0]

    @property
    def spoken_names(self) -> tuple[str, ...]:
        """How a sermon may name this reading: "the Gospel", "our Epistle reading", ..."""
        base = {self.label.lower(), self.heading.lower(), *_LABEL_ALIASES.get(self.label, ())}
        names = set(base)
        for name in base:
            stem = re.sub(r"\s+(?:reading|lesson)$", "", name)
            if stem not in _ORDINAL_WORDS:
                names.update({stem, f"{stem} reading", f"{stem} lesson"})
        return tuple(sorted(names, key=len, reverse=True))


def parse_readings(
    text: str, headings: Mapping[str, str] | None = None, excerpt_chars: int = 500
) -> list[Reading]:
    """Readings in bulletin order; labels repeated in one bulletin get " 2", " 3"."""
    table = sorted((headings or DEFAULT_HEADINGS).items(), key=lambda kv: -len(kv[0]))
    readings: list[tuple[str, str, str, list[str]]] = []
    for line in text.splitlines():
        start = _reading_start(line.strip(), table)
        if start is not None:
            readings.append((*start, []))
        elif readings and line.strip():
            readings[-1][3].append(line.strip())
    result: list[Reading] = []
    used: dict[str, int] = {}
    for label, heading, reference, lines in readings:
        used[label] = used.get(label, 0) + 1
        unique = label if used[label] == 1 else f"{label} {used[label]}"
        body = " ".join(_VERSE_NUMBER.sub("", " ".join(lines)).split())
        result.append(Reading(unique, heading, reference, body[:excerpt_chars].rstrip()))
    return result


def _reading_start(line: str, table: list[tuple[str, str]]) -> tuple[str, str, str] | None:
    folded = line.casefold()
    for heading, label in table:
        if not folded.startswith(heading.casefold()):
            continue
        rest = line[len(heading) :]
        rest = rest[_AFTER_HEADING.match(rest).end() :]  # type: ignore[union-attr]
        parsed = parse_reference(rest)
        if parsed is None and canonical_book(heading) is not None:
            parsed = parse_reference(line)
        if parsed is not None:
            return label, heading, parsed[0]
    return None


def parse_date_line(line: str) -> date | None:
    """A date written like "November 8, 2026", "Nov. 8, 2026" or "8 November 2026"."""
    match = _DATE_LINE.fullmatch(" ".join(line.split()))
    if match is None:
        return None
    month_name = (match["m1"] or match["m2"]).lower()
    day, year = match["d1"] or match["d2"], match["y1"] or match["y2"]
    months = [i for i, m in enumerate(_MONTHS, 1) if m.startswith(month_name)]
    if month_name == "sept":
        months = [9]
    if not months:
        return None
    try:
        return date(int(year), months[0], int(day))
    except ValueError:
        return None


@dataclass(frozen=True)
class BulletinHeader:
    occasion: str
    service_date: date


def parse_header(text: str, headings: Mapping[str, str] | None = None) -> BulletinHeader | None:
    """The occasion and date a bulletin starts with ("Twenty-third Sunday after Trinity",
    then "November 8, 2026"); None unless the first two non-empty paragraphs match."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 2:
        return None
    first, second = lines[0].translate(_STRAIGHT), lines[1]
    table = sorted((headings or DEFAULT_HEADINGS).items(), key=lambda kv: -len(kv[0]))
    if _reading_start(first, table) is not None or parse_date_line(first) is not None:
        return None
    if any(first.casefold() == heading.casefold() for heading, _ in table):
        return None
    when = parse_date_line(second)
    return BulletinHeader(first, when) if when is not None else None
