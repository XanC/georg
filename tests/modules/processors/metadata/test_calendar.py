# SPDX-License-Identifier: AGPL-3.0-only
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from georg.core.errors import ConfigError
from georg.modules.processors.metadata.calendar import Normalized, load_calendar
from georg.modules.processors.metadata.names import series_key, spell_ordinals


@pytest.mark.parametrize(
    ("given", "canonical"),
    [
        ("All Saints Sunday", "All Saints' Day"),
        ("3rd Sunday in Lent", "Third Sunday in Lent"),
        ("21st Sunday after Pentecost", "Twenty-first Sunday after Pentecost"),
        ("third sunday in lent", "Third Sunday in Lent"),
        ("Holy Trinity", "The Holy Trinity"),
        ("All Saints\u2019 Day", "All Saints' Day"),
        ("Lent Midweek III", "Lent Midweek 3"),
    ],
)
def test_alias_replaced_by_canonical_name(given: str, canonical: str) -> None:
    result = load_calendar().normalize(given)
    assert result == Normalized(canonical, True, given if "\u2019" not in given else None)


def test_canonical_name_unchanged() -> None:
    result = load_calendar().normalize("The Day of Pentecost")
    assert result == Normalized("The Day of Pentecost", True)
    assert result.note() is None
    assert load_calendar().normalize("All Saints Sunday").note() == (
        "normalized from 'All Saints Sunday'"
    )


def test_one_year_names_follow_the_lectionary() -> None:
    one_year = load_calendar(lectionary="one-year").normalize("Twenty-third Sunday after Trinity")
    assert one_year == Normalized("Twenty-third Sunday after Trinity", True)
    assert load_calendar(lectionary="one-year").normalize("Laetare").name == (
        "Fourth Sunday in Lent"
    )
    three_year = load_calendar().normalize("Twenty-third Sunday after Trinity")
    assert three_year.name == "Twenty-third Sunday after Trinity"
    assert not three_year.known


def test_unknown_name_passes_through_with_flag_and_suggestion() -> None:
    unknown = load_calendar().normalize("Blessing of the Animals")
    assert unknown == Normalized("Blessing of the Animals", False)
    assert unknown.note() == "not a known day name"
    fuzzy = load_calendar().normalize("Feast of the Holy Trinity")
    assert fuzzy == Normalized("Feast of the Holy Trinity", False, suggestion="The Holy Trinity")
    assert fuzzy.note() == "not a known day name; did you mean 'The Holy Trinity'?"


def test_local_file_overrides_bundled(tmp_path: Path) -> None:
    local = tmp_path / "occasions.toml"
    local.write_text(
        '[canonical]\n"All Saints Sunday" = ["All Saints"]\n"Church Anniversary" = []\n',
        encoding="utf-8",
    )
    calendar = load_calendar(occasions=local)
    assert calendar.normalize("All Saints").name == "All Saints Sunday"
    assert calendar.normalize("All Saints Sunday") == Normalized("All Saints Sunday", True)
    assert calendar.normalize("church anniversary").name == "Church Anniversary"
    assert calendar.normalize("All Saints' Day").name == "All Saints' Day"
    assert calendar.version != load_calendar().version


def test_bad_local_file(tmp_path: Path) -> None:
    local = tmp_path / "occasions.toml"
    local.write_text('[canonical]\n"Day" = "not a list"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="list of aliases"):
        load_calendar(occasions=local)
    local.write_text('[canonical]\n"A" = ["Same"]\n"B" = ["same"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="'same' is listed under 'A' and 'B'"):
        load_calendar(occasions=local)
    with pytest.raises(ConfigError, match="occasions file"):
        load_calendar(occasions=tmp_path / "missing.toml")


def test_bundled_names_are_canonical_spellings() -> None:
    text = resources.files("georg").joinpath("data/calendars/lsb.toml").read_text("utf-8")
    for table, names in tomllib.loads(text).items():
        for name in names:
            assert spell_ordinals(name) == name, (table, name)
            assert series_key(name), name
