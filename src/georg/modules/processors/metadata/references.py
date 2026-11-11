# SPDX-License-Identifier: AGPL-3.0-only
"""Bible references in written ("Ephesians 6:1-4") and spoken form.

Spoken forms are what a transcript of a sermon contains: "Ephesians chapter 6,
verses 1 through 4" or "Ephesians six, one through four". Every reference is
returned as `<Book> <chapter>[:<verses>]` with ASCII hyphens, e.g. "John 18:1-19:42".
"""

import re
from dataclasses import dataclass

DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d"
"""Hyphen-minus, hyphen, non-breaking hyphen, figure/en/em dash, bar, minus signs."""
_DASH = f"[{re.escape(DASHES)}]"

_BOOKS = (
    "Genesis Exodus Leviticus Numbers Deuteronomy Joshua Judges Ruth 1_Samuel 2_Samuel"
    " 1_Kings 2_Kings 1_Chronicles 2_Chronicles Ezra Nehemiah Esther Job Psalm Proverbs"
    " Ecclesiastes Song_of_Solomon Isaiah Jeremiah Lamentations Ezekiel Daniel Hosea Joel"
    " Amos Obadiah Jonah Micah Nahum Habakkuk Zephaniah Haggai Zechariah Malachi Matthew"
    " Mark Luke John Acts Romans 1_Corinthians 2_Corinthians Galatians Ephesians"
    " Philippians Colossians 1_Thessalonians 2_Thessalonians 1_Timothy 2_Timothy Titus"
    " Philemon Hebrews James 1_Peter 2_Peter 1_John 2_John 3_John Jude Revelation"
)
BOOKS: tuple[str, ...] = tuple(name.replace("_", " ") for name in _BOOKS.split())
OLD_TESTAMENT: frozenset[str] = frozenset(BOOKS[: BOOKS.index("Malachi") + 1])
_EXTRA_ALIASES = {
    "Psalm": ("psalms",),
    "Song of Solomon": ("song of songs", "canticles"),
    "Revelation": ("revelations",),
    "Acts": ("acts of the apostles",),
}
_ORDINALS = {
    "1": ("1", "i", "first", "1st"),
    "2": ("2", "ii", "second", "2nd"),
    "3": ("3", "iii", "third", "3rd"),
}
_ORDINAL_KEY = {word: digit for digit, words in _ORDINALS.items() for word in words}


def _alias_key(text: str) -> str:
    words = text.lower().replace(".", " ").split()
    if len(words) > 1 and words[0] in _ORDINAL_KEY:
        words[0] = _ORDINAL_KEY[words[0]]
    return " ".join(words)


def _book_aliases() -> tuple[dict[str, str], list[str]]:
    lookup: dict[str, str] = {}
    patterns: list[str] = []
    for book in BOOKS:
        number, _, base = book.partition(" ") if book[0].isdigit() else ("", "", book)
        names = (base.lower(), *_EXTRA_ALIASES.get(book, ()))
        for name in names:
            words = r"\s+".join(map(re.escape, name.split()))
            if number:
                prefix = "|".join(_ORDINALS[number])
                patterns.append(rf"(?:{prefix})\.?\s*{words}")
                lookup[f"{number} {name}"] = book
            else:
                patterns.append(words)
                lookup[name] = book
    patterns.sort(key=len, reverse=True)
    return lookup, patterns


_BOOK_LOOKUP, _BOOK_PATTERNS = _book_aliases()
BOOK_RE = re.compile(rf"(?<![\w])(?:{'|'.join(_BOOK_PATTERNS)})(?![\w])", re.IGNORECASE)

_V = r"\d{1,3}[a-c]?(?!\d)"
_VERSE_PART = rf"{_V}(?:\s*{_DASH}\s*(?:\d{{1,3}}\s*:\s*)?{_V})?"
_WRITTEN = re.compile(
    rf"\s*(?P<chapter>\d{{1,3}})(?!\d)"
    rf"(?:\s*:\s*(?P<verses>{_VERSE_PART}(?:\s*,\s*{_VERSE_PART}(?!\s*:))*)"
    rf"|\s*{_DASH}\s*(?P<chapter_end>\d{{1,3}})(?![\d:]))?"
)
_TOKEN = re.compile(rf"\d{{1,3}}[a-c]?(?!\d)|[a-z]+(?:-[a-z]+)*|{_DASH}", re.IGNORECASE)

_UNITS = {
    w: i
    for i, w in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
        ]
    )
}
_TENS = {
    w: 10 * i
    for i, w in enumerate(
        ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"], 2
    )
}
_RANGE_WORDS = frozenset({"through", "thru", "to", "till", "until"})
_START_WORDS = frozenset({"beginning", "starting", "from"})


@dataclass(frozen=True)
class BookMention:
    book: str
    start: int
    end: int


def normalize_reference(reference: str) -> str:
    """ASCII hyphens, no space around hyphens or colons, ", " between verse groups."""
    text = re.sub(_DASH, "-", reference)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s*:\s*", ":", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    return " ".join(text.split())


def canonical_book(name: str) -> str | None:
    return _BOOK_LOOKUP.get(_alias_key(name))


def find_books(text: str) -> list[BookMention]:
    """Every book name in `text`, in order."""
    found = []
    for match in BOOK_RE.finditer(text):
        book = canonical_book(match.group(0))
        if book is not None:
            found.append(BookMention(book, match.start(), match.end()))
    return found


def reference_after(text: str, mention: BookMention) -> tuple[str, int] | None:
    """The reference starting at a book mention, and where it ends; needs a chapter."""
    rest = text[mention.end :]
    written = _WRITTEN.match(rest)
    if written and not re.match(r"\s*\d{1,3}\s*,?\s+(?:chapter|verses?)\b", rest, re.I):
        chapter, verses, chapter_end = written.group("chapter", "verses", "chapter_end")
        if int(chapter) == 0:
            return None
        ref = f"{mention.book} {chapter}"
        if verses:
            ref += ":" + verses
        elif chapter_end:
            ref += "-" + chapter_end
        return normalize_reference(ref), mention.end + written.end()
    spoken = _spoken(rest)
    if spoken is None:
        return None
    ref, used = spoken
    return f"{mention.book} {ref}", mention.end + used


def parse_reference(text: str) -> tuple[str, int] | None:
    """A reference at the start of `text` (after optional spaces), and where it ends."""
    match = BOOK_RE.match(text, len(text) - len(text.lstrip()))
    if match is None:
        return None
    book = canonical_book(match.group(0))
    if book is None:
        return None
    return reference_after(text, BookMention(book, match.start(), match.end()))


def find_references(text: str) -> list[tuple[str, int, int]]:
    """Every reference with a chapter in `text`: (reference, start, end)."""
    found = []
    position = 0
    for mention in find_books(text):
        if mention.start < position:
            continue
        parsed = reference_after(text, mention)
        if parsed is not None:
            found.append((parsed[0], mention.start, parsed[1]))
            position = parsed[1]
    return found


def spoken_number(words: list[str], i: int) -> tuple[int, int] | None:
    """The number written in digits or English words at `words[i]`, and the next index."""
    if i >= len(words):
        return None
    word = words[i].lower()
    if word[:1].isdigit():
        return int(re.sub(r"\D", "", word)), i + 1
    value = _word_value(word)
    if value is None:
        return None
    i += 1
    if value in _TENS.values() and i < len(words) and 0 < _UNITS.get(words[i].lower(), 0) < 10:
        value += _UNITS[words[i].lower()]
        i += 1
    if value < 10 and i < len(words) and words[i].lower() == "hundred":
        value *= 100
        i += 1
        j = i + 1 if i < len(words) and words[i].lower() == "and" else i
        rest = spoken_number(words, j)
        if rest is not None and rest[0] < 100:
            value, i = value + rest[0], rest[1]
    return value, i


def _word_value(word: str) -> int | None:
    parts = word.split("-")
    if len(parts) == 2 and parts[0] in _TENS and 0 < _UNITS.get(parts[1], 0) < 10:
        return _TENS[parts[0]] + _UNITS[parts[1]]
    if len(parts) == 1 and word != "zero":
        return _UNITS.get(word, _TENS.get(word))
    return None


def _spoken(text: str) -> tuple[str, int] | None:
    """`chapter[:verses]` from a spoken reference at the start of `text`, and its length."""
    matches = list(_TOKEN.finditer(text[:200]))
    words = [m.group(0).lower() for m in matches]
    if not words or (matches[0].start() and text[: matches[0].start()].strip(" ,")):
        return None

    def at(i: int, *options: str) -> bool:
        return i < len(words) and words[i] in options

    i = 1 if at(0, "chapter") else 0
    chapter = spoken_number(words, i)
    if chapter is None:
        return None
    ch, i = chapter
    if ch == 0:
        return None
    ref = str(ch)
    end = i
    j = i
    if at(j, *_START_WORDS):
        j += 1
        j += 1 if at(j, "at", "with") else 0
    j += 1 if at(j, "verse", "verses") else 0
    first = spoken_number(words, j)
    if first is not None and (j > i or i > 1 or not at(0, "chapter")):
        verses = str(first[0])
        j = first[1]
        end = j
        if at(j, *_RANGE_WORDS) or (j < len(words) and re.fullmatch(_DASH, words[j])):
            k = j + 1
            other_chapter = None
            if at(k, "chapter"):
                found = spoken_number(words, k + 1)
                if found is not None:
                    other_chapter, k = found
                    k += 1 if at(k, "verse", "verses") else 0
            last = spoken_number(words, k)
            if last is not None:
                prefix = f"{other_chapter}:" if other_chapter is not None else ""
                verses += f"-{prefix}{last[0]}"
                end = last[1]
        ref += ":" + verses
    return ref, matches[end - 1].end()
