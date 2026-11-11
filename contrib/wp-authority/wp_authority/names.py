# SPDX-License-Identifier: AGPL-3.0-only
"""Comparison keys and ordinal spelling for series and speaker names."""

import re

from wp_authority.scripture import clean_name

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


def speaker_key(name: str) -> str:
    """Equal for names that differ only in case, spacing and punctuation ("De Young")."""
    return re.sub(r"[\W_]", "", clean_name(name).lower())


_TITLES = frozenset(
    [
        "rev",
        "reverend",
        "dr",
        "pastor",
        "vicar",
        "seminarian",
        "deacon",
        "deaconess",
        "bishop",
        "father",
        "fr",
        "mr",
        "mrs",
        "ms",
        "sem",
        "intern",
        "elder",
        "president",
        "prof",
        "professor",
        "the",
    ]
)


def person_name(name: str) -> str | None:
    """ "Rev. Dr. Mark S. Nuckols, Senior Pastor" -> "Mark Nuckols"; None if unclear."""
    text = clean_name(name).split(",")[0]
    words = [w for w in re.split(r"\s+", text) if w]
    while words and words[0].lower().rstrip(".") in _TITLES:
        words.pop(0)
    words = [w for w in words if not re.fullmatch(r"[A-Za-z]\.?", w)]
    if len(words) < 2:
        return None
    return f"{words[0]} {words[-1]}"
