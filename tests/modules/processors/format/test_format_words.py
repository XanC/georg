# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from georg.modules.processors.format import compare_word_sequences, tokenize

ORIGINAL = "grace to you and peace from god our father and the lord jesus christ"


@pytest.mark.parametrize(
    "formatted",
    [
        ORIGINAL,
        "Grace to you, and peace, from God our Father and the Lord Jesus Christ.",
        "Grace to you and peace\n\nfrom God our *Father* and the **Lord** Jesus Christ!",
        "GRACE TO YOU AND PEACE — FROM GOD OUR FATHER AND THE LORD JESUS CHRIST",
        '"Grace to you and peace," from God our Father... and the Lord Jesus Christ?',
    ],
)
def test_punctuation_case_and_markup_are_ignored(formatted: str) -> None:
    result = compare_word_sequences(ORIGINAL, formatted)
    assert result.preserved and result.discrepancies == ()
    assert result.original_words == result.formatted_words == 14


@pytest.mark.parametrize(
    ("original", "formatted"),
    [
        ("a well known story", "a well-known story"),
        ("twenty-one people", "twenty one people"),
        ("dont do that", "don't do that"),
        ("it\u2019s fine", "it's fine"),
        ("john 3:16 says", "John 3:16 says"),
        ("about 1000 people", "about 1,000 people"),
        ("verses 1 2 and 3", "verses 1, 2, and 3"),
        ("Café", "CAFÉ"),
    ],
)
def test_hyphens_apostrophes_and_numbers(original: str, formatted: str) -> None:
    assert compare_word_sequences(original, formatted).preserved


def test_inserted_word() -> None:
    result = compare_word_sequences("peace be with you", "peace be always with you")
    assert not result.preserved
    assert result.discrepancies == ('word 3: inserted "always"',)


def test_removed_word() -> None:
    result = compare_word_sequences("peace be with you", "Peace be with.")
    assert result.discrepancies == ('word 4: removed "you"',)


def test_changed_word() -> None:
    result = compare_word_sequences("we was there", "We were there.")
    assert result.discrepancies == ('word 2: changed "was" to "were"',)


def test_number_spelled_out_is_a_change() -> None:
    result = compare_word_sequences("three wise men", "3 wise men")
    assert result.discrepancies == ('word 1: changed "three" to "3"',)


def test_reordered_words_are_reported() -> None:
    assert not compare_word_sequences("one two three", "two one three").preserved


def test_long_runs_and_many_differences_are_truncated() -> None:
    long_gone = compare_word_sequences(" ".join(f"w{i}" for i in range(20)), "")
    assert long_gone.discrepancies == ('word 1: removed "w0 w1 w2 w3 w4 w5 w6 w7" (+12 more)',)
    many = compare_word_sequences(
        " ".join(f"a{i} keep{i}" for i in range(10)),
        " ".join(f"b{i} keep{i}" for i in range(10)),
        max_discrepancies=3,
    )
    assert len(many.discrepancies) == 4
    assert many.discrepancies[-1] == "... and 7 more differences"


def test_empty_inputs() -> None:
    assert compare_word_sequences("", "  \n").preserved
    assert tokenize("") == []
    assert tokenize("__Amen__!") == ["amen"]
