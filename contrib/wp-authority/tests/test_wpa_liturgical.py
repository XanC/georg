# SPDX-License-Identifier: AGPL-3.0-only
from datetime import date, timedelta

import pytest

from wp_authority.liturgical import advent1, church_year, days, easter, series, table_for
from wp_authority.names import ordinal, person_name, series_key, speaker_key, spell_ordinals


def names(day: str) -> list[str]:
    return [d.name for d in days(date.fromisoformat(day))]


def meeus_easter(year: int) -> date:
    """The owner's reference: Meeus/Jones/Butcher."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    return date(year, (h + m - 7 * n + 114) // 31, (h + m - 7 * n + 114) % 31 + 1)


def owner_advent_1(year: int) -> date:
    christmas = date(year, 12, 25)
    days_before = (christmas.weekday() - 6) % 7 or 7
    return christmas - timedelta(days_before) - timedelta(weeks=3)


def test_easter_and_advent_match_reference() -> None:
    for year in range(1900, 2101):
        assert easter(year) == meeus_easter(year)
        assert advent1(year) == owner_advent_1(year)
    assert [easter(y) for y in (2008, 2019, 2024, 2025, 2026)] == [
        date(2008, 3, 23),
        date(2019, 4, 21),
        date(2024, 3, 31),
        date(2025, 4, 20),
        date(2026, 4, 5),
    ]


def test_church_year_and_series() -> None:
    assert church_year(date(2025, 11, 30)) == 2026
    assert church_year(date(2025, 11, 23)) == 2025
    assert [series(date(y, 6, 1)) for y in (2017, 2018, 2019, 2020, 2026)] == list("ABCAA")
    assert series(date(2025, 12, 7)) == "A"
    assert table_for(date(2024, 9, 1)) == "lsb-3b"
    assert table_for(date(2025, 3, 2)) == "lsb-3c"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2025-11-30", ["First Sunday in Advent", "St. Andrew, Apostle"]),
        ("2025-12-21", ["Fourth Sunday in Advent", "St. Thomas, Apostle"]),
        ("2025-12-24", ["Christmas Eve"]),
        ("2025-12-25", ["Christmas Day"]),
        ("2025-12-28", ["First Sunday after Christmas", "The Holy Innocents, Martyrs"]),
        ("2026-01-04", ["Second Sunday after Christmas"]),
        ("2026-01-06", ["The Epiphany of Our Lord"]),
        ("2026-01-11", ["The Baptism of Our Lord"]),
        ("2026-01-18", ["Second Sunday after the Epiphany", "The Confession of St. Peter"]),
        ("2026-02-15", ["The Transfiguration of Our Lord"]),
        ("2026-02-18", ["Ash Wednesday"]),
        ("2026-02-22", ["First Sunday in Lent"]),
        ("2026-03-29", ["Palm Sunday"]),
        ("2026-03-30", ["Monday of Holy Week"]),
        ("2026-04-02", ["Holy Thursday"]),
        ("2026-04-03", ["Good Friday"]),
        ("2026-04-04", ["Holy Saturday"]),
        ("2026-04-05", ["The Resurrection of Our Lord"]),
        ("2026-04-12", ["Second Sunday of Easter"]),
        ("2026-05-14", ["The Ascension of Our Lord"]),
        ("2026-05-24", ["The Day of Pentecost"]),
        ("2026-05-31", ["The Holy Trinity", "The Visitation"]),
        ("2026-06-28", ["Fifth Sunday after Pentecost"]),
        ("2025-10-26", ["Twentieth Sunday after Pentecost", "Reformation Day"]),
        ("2026-11-01", ["Twenty-third Sunday after Pentecost", "All Saints' Day"]),
        ("2025-11-23", ["Twenty-fourth Sunday after Pentecost"]),
        ("2026-03-04", []),
        ("2025-10-31", []),
        ("2026-11-26", []),
    ],
)
def test_days(day: str, expected: list[str]) -> None:
    assert names(day) == expected


def test_palm_sunday_names() -> None:
    (palm,) = days(date(2026, 3, 29))
    assert "Sunday of the Passion" in palm.names


def test_proper_and_readings() -> None:
    (day,) = days(date(2025, 8, 3))
    assert day.key == "Proper 13"
    assert "Luke 12:13-21" in day.readings()
    (last,) = days(date(2025, 11, 23))
    assert last.key == "Proper 29"
    assert "Last Sunday of the Church Year" in last.names


def test_ordinals_and_keys() -> None:
    assert [ordinal(n) for n in (1, 3, 12, 20, 21, 26)] == [
        "First",
        "Third",
        "Twelfth",
        "Twentieth",
        "Twenty-first",
        "Twenty-sixth",
    ]
    assert spell_ordinals("22nd Sunday after Pentecost") == "Twenty-second Sunday after Pentecost"
    assert series_key("4th Sunday of Advent") == series_key("Fourth Sunday in Advent")
    assert series_key("The Holy Trinity") == series_key("Holy Trinity")
    assert series_key("St Michael and All Angels") == series_key("St. Michael &amp; All Angels")
    assert series_key("Third Sunday after Epiphany") == series_key(
        "Third Sunday after the Epiphany"
    )
    assert series_key("Day of Pentecost") != series_key("Pentecost Sunday")
    assert speaker_key("Seminarian Mark De Young") == speaker_key("Seminarian Mark DeYoung")
    assert person_name("Rev. Dr. Mark S. Nuckols, Senior Pastor") == "Mark Nuckols"
    assert person_name("Vicar Ross Davis") == "Ross Davis"
    assert person_name("Nobody") is None
