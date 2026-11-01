# SPDX-License-Identifier: AGPL-3.0-only
"""Check that formatting changed only punctuation, case, and whitespace.

Tokens are case-folded runs of letters and digits. Apostrophes are dropped
("don't" == "dont"), punctuation between digits is dropped ("1,000" == "1000",
"3:16" == "316"), and everything else, including hyphens and dashes, separates words
("well-known" == "well known"). Markdown emphasis characters are punctuation too.
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass

_APOSTROPHES = re.compile(r"['\u2018\u2019\u02bc`]")
_DIGIT_PUNCT = re.compile(r"(?<=\d)[.,:](?=\d)")
_TOKEN = re.compile(r"[^\W_]+")
_CONTEXT_WORDS = 8


@dataclass(frozen=True)
class WordComparison:
    preserved: bool
    discrepancies: tuple[str, ...]
    original_words: int
    formatted_words: int


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _APOSTROPHES.sub("", text)
    text = _DIGIT_PUNCT.sub("", text)
    return _TOKEN.findall(text)


def _quote(words: list[str]) -> str:
    shown = " ".join(words[:_CONTEXT_WORDS])
    more = len(words) - _CONTEXT_WORDS
    return f'"{shown}"' + (f" (+{more} more)" if more > 0 else "")


def compare_word_sequences(
    original: str, formatted: str, *, max_discrepancies: int = 50
) -> WordComparison:
    """Compare word sequences; describe each difference by its 1-based original word index."""
    a = tokenize(original)
    b = tokenize(formatted)
    found: list[str] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        where = f"word {i1 + 1}"
        if tag == "replace":
            found.append(f"{where}: changed {_quote(a[i1:i2])} to {_quote(b[j1:j2])}")
        elif tag == "delete":
            found.append(f"{where}: removed {_quote(a[i1:i2])}")
        else:
            found.append(f"{where}: inserted {_quote(b[j1:j2])}")
    if len(found) > max_discrepancies:
        extra = len(found) - max_discrepancies
        found = [*found[:max_discrepancies], f"... and {extra} more differences"]
    return WordComparison(
        preserved=not found,
        discrepancies=tuple(found),
        original_words=len(a),
        formatted_words=len(b),
    )
