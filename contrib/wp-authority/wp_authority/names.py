# SPDX-License-Identifier: AGPL-3.0-only
"""Comparison keys for speaker names; series-name helpers come from Georg."""

import re

from georg.modules.processors.metadata.names import (
    clean_name,
    has_numeric_ordinal,
    ordinal,
    series_key,
    spell_ordinals,
)

__all__ = [
    "has_numeric_ordinal",
    "ordinal",
    "person_name",
    "series_key",
    "speaker_key",
    "spell_ordinals",
]


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
