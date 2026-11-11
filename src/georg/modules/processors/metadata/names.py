# SPDX-License-Identifier: AGPL-3.0-only
"""Comparison keys and ordinal spelling for day (series) names.

Shared with contrib/wp-authority, which imports these rather than keeping a copy.
"""

import html
import re

_UNITS = [
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
    "eleventh",
    "twelfth",
    "thirteenth",
    "fourteenth",
    "fifteenth",
    "sixteenth",
    "seventeenth",
    "eighteenth",
    "nineteenth",
]
_TENS = {20: "twent", 30: "thirt"}


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


def ordinal(n: int) -> str:
    """1 -> "First", 21 -> "Twenty-first" (1..39)."""
    if not 1 <= n <= 39:
        raise ValueError(n)
    if n < 20:
        return _UNITS[n - 1].capitalize()
    tens, unit = divmod(n, 10)
    stem = _TENS[tens * 10]
    return f"{stem}ieth".capitalize() if unit == 0 else f"{stem}y-{_UNITS[unit - 1]}".capitalize()


def spell_ordinals(text: str) -> str:
    """ "3rd Sunday" -> "Third Sunday"; numbers outside 1..39 are left alone."""

    def word(match: re.Match[str]) -> str:
        n = int(match[1])
        return ordinal(n) if 1 <= n <= 39 else match[0]

    return re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", word, text, flags=re.I)


def has_numeric_ordinal(text: str) -> bool:
    return re.search(r"\b\d{1,2}(?:st|nd|rd|th)\b", text, re.I) is not None


def series_key(name: str) -> str:
    """Equal for names that differ only in in/of, "the", St/St./Saint, ordinals, case,
    punctuation, a "Feast of"/"Festival of" prefix or a "Day"/"(Observed)" suffix."""
    text = spell_ordinals(clean_name(name)).lower().replace("\u2019", "'")
    text = text.replace("&", " and ")
    text = re.sub(r"\b(?:st|saint)\b\.?", "st", text)
    text = re.sub(r"^(?:the )?(?:feast|festival) of ", "", text.strip())
    text = re.sub(r"\s*\(observed\)$", "", text)
    text = re.sub(r"\s+day$", "", text)
    text = re.sub(r"[^\w\s]", " ", text.replace("'", "")).replace("_", " ")
    words = ["in" if w == "of" else w for w in text.split() if w != "the"]
    return " ".join(words)
