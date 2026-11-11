# SPDX-License-Identifier: AGPL-3.0-only
"""Split composite `ctc_sermon_book` terms into one normalized reference each.

A segment (between "," or ";") that starts with a book name starts a new reference;
verse ("16-21") and chapter ("2:18-26") continuations stay with the reference before
them. Anything that doesn't fit that grammar is rejected, never guessed.
"""

import html
import re
from dataclasses import dataclass

from georg.modules.processors.metadata.references import BOOK_RE, DASHES, canonical_book

_V = r"\d{1,3}[a-c]?"
_VERSES = rf"{_V}(?:-(?:\d{{1,3}}:)?{_V})?"
_FIRST = re.compile(rf"^(?P<c>\d{{1,3}})(?::(?P<v>{_VERSES})|-(?P<c2>\d{{1,3}}))?$")
_VERSE_CONT = re.compile(rf"^{_VERSES}$")
_CHAPTER_CONT = re.compile(rf"^\d{{1,3}}:{_VERSES}$")
_SAME_CHAPTER = re.compile(r"^(\d{1,3}):(\d{1,3}[a-c]?)-\1:(\d{1,3}[a-c]?)$")
_ALLOWED = re.compile(r"^[A-Za-z0-9 .:,;\-]+$")
_ZERO = re.compile(r"(?<!\d)0+(?!\d)")


class Unparseable(ValueError):
    """A term that needs a human."""


def clean_name(name: str) -> str:
    """Decode HTML entities (repeatedly) and UTF-8 read as Latin-1/cp1252 (mojibake)."""
    previous = None
    while previous != name:
        previous, name = name, html.unescape(name)
    if any(ch in name for ch in "\u00c2\u00e2\u00c3"):
        for codec in ("cp1252", "latin-1"):
            try:
                name = name.encode(codec).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
    return name.strip()


def _ascii(text: str) -> str:
    text = re.sub(f"[{re.escape(DASHES)}]", "-", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s*:\s*", ":", text)
    return " ".join(text.split())


@dataclass
class _Ref:
    book: str
    parts: list[str]
    has_verses: bool

    def text(self) -> str:
        return f"{self.book} {', '.join(self.parts)}"


def _simplify(part: str) -> str:
    same = _SAME_CHAPTER.match(part)
    return f"{same[1]}:{same[2]}-{same[3]}" if same else part


def split_references(name: str, dash: str = "-") -> list[str]:
    """One normalized reference per Bible passage in a term name; raises Unparseable."""
    text = _ascii(clean_name(name))
    if not text or not _ALLOWED.match(text):
        raise Unparseable(f"unexpected characters in {name!r}")
    refs: list[_Ref] = []
    for raw in re.split(r"[,;]", text):
        segment = raw.strip()
        if not segment:
            continue
        match = BOOK_RE.match(segment)
        book = canonical_book(match.group(0)) if match else None
        if match and book:
            rest = segment[match.end() :].strip().removeprefix(".").strip()
            first = _FIRST.match(rest)
            if not first or _ZERO.search(rest):
                raise Unparseable(f"cannot read {segment!r} in {name!r}")
            refs.append(_Ref(book, [_simplify(rest)], first["v"] is not None))
        elif refs and refs[-1].has_verses and not _ZERO.search(segment):
            if _VERSE_CONT.match(segment) or _CHAPTER_CONT.match(segment):
                refs[-1].parts.append(_simplify(segment))
            else:
                raise Unparseable(f"cannot read {segment!r} in {name!r}")
        else:
            raise Unparseable(f"{segment!r} in {name!r} has no book")
    if not refs:
        raise Unparseable(f"no reference in {name!r}")
    out: list[str] = []
    for ref in refs:
        value = ref.text().replace("-", dash)
        if value not in out:
            out.append(value)
    return out


Span = tuple[str, int, int]


def spans(reference: str) -> list[Span]:
    """(book, start, end) positions (chapter * 1000 + verse) covered by one reference."""
    text = _ascii(reference)
    match = BOOK_RE.match(text)
    book = canonical_book(match.group(0)) if match else None
    if not match or not book:
        return []
    out: list[Span] = []
    chapter = 0
    for raw in text[match.end() :].split(","):
        part = re.sub(r"(?<=\d)[a-c]", "", raw.strip())
        start, _, end = part.partition("-")
        if ":" in start:
            c, v = start.split(":")
            chapter = int(c)
            lo = chapter * 1000 + int(v)
        elif chapter and out:
            lo = chapter * 1000 + int(start)
        else:
            chapter = int(start)
            lo = chapter * 1000
            if not end:
                out.append((book, lo, lo + 999))
                continue
            out.append((book, lo, int(end) * 1000 + 999))
            continue
        if not end:
            hi = lo
        elif ":" in end:
            c, v = end.split(":")
            chapter = int(c)
            hi = chapter * 1000 + int(v)
        else:
            hi = chapter * 1000 + int(end)
        out.append((book, lo, hi))
    return out


def overlaps(a: list[str], b: list[str]) -> bool:
    """Whether any passage in `a` shares a verse with any passage in `b`."""
    left = [s for ref in a for s in spans(ref)]
    right = [s for ref in b for s in spans(ref)]
    return any(x[0] == y[0] and x[1] <= y[2] and y[1] <= x[2] for x in left for y in right)
