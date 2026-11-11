# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from wp_authority.scripture import Unparseable, clean_name, overlaps, split_references


@pytest.mark.parametrize(
    ("term", "refs"),
    [
        (
            "Luke 12:13-21, Ecclesiastes 1:2, 12-14, 2:18-26",
            ["Luke 12:13-21", "Ecclesiastes 1:2, 12-14, 2:18-26"],
        ),
        ("James 2:1-10, 14-18; Mark 7:24-37", ["James 2:1-10, 14-18", "Mark 7:24-37"]),
        ("Mark 6:1-6, 16-21", ["Mark 6:1-6, 16-21"]),
        ("Daniel 10:10-14; 12:1-3", ["Daniel 10:10-14, 12:1-3"]),
        ("I John 3:1-7", ["1 John 3:1-7"]),
        ("II Kings 5:1-14; 1 Corinthians 9:24-27", ["2 Kings 5:1-14", "1 Corinthians 9:24-27"]),
        ("1st Peter 3:13-22", ["1 Peter 3:13-22"]),
        ("Acts 2:14a, 22-36", ["Acts 2:14a, 22-36"]),
        ("Genesis 18:1-10a, 10b-14", ["Genesis 18:1-10a, 10b-14"]),
        ("Romans 3:23-4:21", ["Romans 3:23-4:21"]),
        ("2 Corinthians 4:13 \u2013 5:1", ["2 Corinthians 4:13-5:1"]),
        ("Mark 10: 2-16", ["Mark 10:2-16"]),
        ("Isaiah 61:1-4,8-11", ["Isaiah 61:1-4, 8-11"]),
        ("John 11:17-11:27", ["John 11:17-27"]),
        ("John 15-17", ["John 15-17"]),
        ("Psalm 23", ["Psalm 23"]),
        ("Psalms 23; Psalm 23", ["Psalm 23"]),
        ("John 6:22\u00e2\u0080\u009335", ["John 6:22-35"]),
        ("Ruth 1:1&ndash;19a", ["Ruth 1:1-19a"]),
    ],
)
def test_split(term: str, refs: list[str]) -> None:
    assert split_references(term) == refs


def test_en_dash_output() -> None:
    assert split_references("James 2:1-10, 14-18; Mark 7:24-37", "\u2013") == [
        "James 2:1\u201310, 14\u201318",
        "Mark 7:24\u201337",
    ]


@pytest.mark.parametrize(
    "term",
    [
        "?",
        "_",
        "39-43",
        "Third Sunday of Easter",
        "Samuel 2:12-17",
        "Ruth 1:-19a",
        "Genesis 0:1",
        "Hebrews",
        "John 15, 16",
        "(Ephesians 1:15-23) Ephesians 2:11-22",
    ],
)
def test_unparseable(term: str) -> None:
    with pytest.raises(Unparseable):
        split_references(term)


def test_clean_name() -> None:
    assert clean_name("All Saints&amp;apos; Day") == "All Saints' Day"
    assert clean_name(" Isaiah 64:1\u00e2\u0080\u00939 ") == "Isaiah 64:1\u20139"


def test_overlaps() -> None:
    assert overlaps(["John 3:1-17"], ["John 3:14-21"])
    assert not overlaps(["John 3:1-13"], ["John 3:14-21"])
    assert overlaps(["John 3"], ["John 3:16"])
    assert overlaps(["Romans 3:23-4:21"], ["Romans 4:1-5"])
    assert overlaps(["Acts 2:14a, 22-36"], ["Acts 2:36"])
    assert not overlaps(["1 John 3:1-3"], ["John 3:1-3"])
