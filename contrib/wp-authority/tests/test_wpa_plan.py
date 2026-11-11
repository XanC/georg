# SPDX-License-Identifier: AGPL-3.0-only
import csv
import json
from pathlib import Path
from typing import Any

import pytest

from wp_authority.mapping import Mapping
from wp_authority.plan import build_plan, write_csv

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.json"
EXAMPLE = Path(__file__).parents[1] / "mapping.example.toml"


def snapshot() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text("utf-8"))
    return data


def mapping(**extra: Any) -> Mapping:
    base = Mapping.load(EXAMPLE).model_dump()
    for section, values in extra.items():
        for key, value in values.items():
            base[section][key] = value
    return Mapping.model_validate(base)


def terms(plan: dict[str, Any], action: str, taxonomy: str = "ctc_sermon_book") -> dict[str, Any]:
    return {
        r["name"]: r["into"]
        for r in plan["terms"]
        if r["action"] == action and r["taxonomy"] == taxonomy
    }


def change(plan: dict[str, Any], post: int, taxonomy: str) -> tuple[list[str], list[str]]:
    (row,) = [r for r in plan["sermon_changes"] if (r["post"], r["taxonomy"]) == (post, taxonomy)]
    return row["before"], row["after"]


def test_scripture() -> None:
    plan = build_plan(snapshot(), mapping())
    assert change(plan, 100, "ctc_sermon_book")[1] == [
        "Luke 12:13-21",
        "Ecclesiastes 1:2, 12-14, 2:18-26",
    ]
    assert terms(plan, "create") == {
        "Ecclesiastes 1:2, 12-14, 2:18-26": [],
        "James 2:1-10, 14-18": [],
        "Mark 7:24-37": [],
    }
    assert terms(plan, "rename") == {"Mark 10: 2-16": ["Mark 10:2-16"]}
    assert terms(plan, "merge") == {"Isaiah 61:1-4,8-11": ["Isaiah 61:1-4, 8-11"]}
    assert "Luke 12:13-21, Ecclesiastes 1:2, 12-14, 2:18-26" in terms(plan, "delete-emptied")
    assert set(terms(plan, "delete-unused")) == {"Romans 3:19-28", "John 2:1-11"}
    human = {h["name"] for h in plan["needs_human"] if h["kind"] == "scripture"}
    assert human == {"?", "Third Sunday of Easter"}
    renamed = [
        r for r in plan["sermon_changes"] if r["post"] == 102 and r["taxonomy"] == "ctc_sermon_book"
    ]
    assert renamed == []


def test_scripture_fix_dash_and_nesting() -> None:
    plan = build_plan(
        snapshot(),
        mapping(
            scripture={
                "dash": "\u2013",
                "nest_under_books": True,
                "fix": {"Third Sunday of Easter": [], "?": ["Matthew 5:1-12"]},
            }
        ),
    )
    assert change(plan, 108, "ctc_sermon_book") == (["Third Sunday of Easter"], [])
    assert change(plan, 106, "ctc_sermon_book") == (["?"], ["Matthew 5:1\u201312"])
    assert "Mark 10:2\u201316" in terms(plan, "rename")["Mark 10: 2-16"]
    parents = terms(plan, "parent")
    assert parents["Mark 10:2\u201316"] == ["Mark"]
    assert parents["Psalm 23"] == ["Psalm"]
    assert {"Mark", "Psalm", "Luke"} <= set(terms(plan, "create"))
    assert not [h for h in plan["needs_human"] if h["kind"] == "scripture"]


def test_series_canonical_and_suggestions() -> None:
    plan = build_plan(snapshot(), mapping())
    assert terms(plan, "rename", "ctc_sermon_series") == {"All Saints Sunday": ["All Saints' Day"]}
    assert change(plan, 106, "ctc_sermon_series") == (["All Saints"], ["All Saints' Day"])
    deleted = terms(plan, "delete-unused", "ctc_sermon_series")
    assert set(deleted) == {"Reformation Sunday", "Unused Day"}
    assert "All Saints" in terms(plan, "delete-emptied", "ctc_sermon_series")
    suggested = plan["suggestions"]["series"]
    assert suggested["Third Sunday after Pentecost"] == ["3rd Sunday after Pentecost"]
    assert suggested["Holy Trinity"] == ["The Holy Trinity"]
    assert any(
        {"Fourth Sunday in Advent", "Fourth Sunday of Advent"} == {k, *v}
        for k, v in suggested.items()
    )
    assert not any(
        r["taxonomy"] == "ctc_sermon_series" and r["post"] in (102, 110, 111)
        for r in plan["sermon_changes"]
    )


def test_speakers_people_and_suggestions() -> None:
    plan = build_plan(
        snapshot(),
        mapping(speakers={"people": {"Ross Davis": ["Vicar Ross Davis", "Rev. Ross Davis"]}}),
    )
    assert terms(plan, "create", "ctc_sermon_speaker") == {"Ross Davis": []}
    assert terms(plan, "parent", "ctc_sermon_speaker") == {
        "Vicar Ross Davis": ["Ross Davis"],
        "Rev. Ross Davis": ["Ross Davis"],
    }
    assert set(terms(plan, "delete-unused", "ctc_sermon_speaker")) == {"Nobody"}
    assert plan["suggestions"]["speakers"] == {}
    assert terms(plan, "merge", "ctc_sermon_speaker") == {
        "Seminarian Mark DeYoung": ["Seminarian Mark De Young"]
    }
    unmapped = build_plan(snapshot(), Mapping())
    assert unmapped["suggestions"]["speakers"] == {
        "Seminarian Mark DeYoung": ["Seminarian Mark De Young"]
    }
    assert plan["suggestions"]["people"] == {}
    creates = [
        a for a in plan["actions"] if a["op"] == "create" and a["taxonomy"] == "ctc_sermon_speaker"
    ]
    assert creates == [
        {
            "op": "create",
            "taxonomy": "ctc_sermon_speaker",
            "ref": "new:ctc_sermon_speaker:Ross Davis",
            "name": "Ross Davis",
        }
    ]
    described = build_plan(
        snapshot(),
        mapping(
            speakers={
                "people": {"Ross Davis": {"terms": ["Vicar Ross Davis"], "description": "Pastor"}}
            }
        ),
    )
    (describe,) = [a for a in described["actions"] if a["op"] == "describe"]
    assert describe["term"] == "new:ctc_sermon_speaker:Ross Davis"
    assert describe["description"] == "Pastor"
    assert not [a for a in plan["actions"] if a["op"] == "describe"]
    plain = build_plan(snapshot(), mapping())
    assert plain["suggestions"]["people"] == {"Ross Davis": ["Rev. Ross Davis", "Vicar Ross Davis"]}


def test_series_fill_two_clues() -> None:
    plan = build_plan(snapshot(), mapping())
    fills = {f["post"]: f for f in plan["series_fill"]}
    assert fills[100]["series"] == "Eighth Sunday after Pentecost"
    assert fills[100]["clues"] == ["date", "scripture"]
    assert fills[100]["new_term"]
    assert fills[107]["series"] == "Reformation Day"
    assert fills[107]["clues"] == ["date", "title"]
    assert not fills[107]["new_term"]
    human = {h["post"]: h for h in plan["needs_human"] if h["kind"] == "series"}
    assert set(human) == {101, 109}
    assert human[101]["candidates"] == ["Thirteenth Sunday after Pentecost"]
    assert human[109]["candidates"] == ["Second Sunday after the Epiphany"]
    assert fills[100]["lectionary"] == "three-year C"
    assert not any(
        a["taxonomy"] == "ctc_sermon_series" and a.get("post") in (100, 107)
        for a in plan["actions"]
    )


def test_series_assign_is_applied_explicitly() -> None:
    plan = build_plan(snapshot(), mapping(series={"assign": {"100": "8th Sunday after Pentecost"}}))
    assert change(plan, 100, "ctc_sermon_series") == ([], ["8th Sunday after Pentecost"])
    assert 100 not in {f["post"] for f in plan["series_fill"]}


def test_actions_order_and_csv(tmp_path: Path) -> None:
    plan = build_plan(snapshot(), mapping())
    ops = [a["op"] for a in plan["actions"]]
    assert ops == sorted(
        ops, key=["create", "rename", "parent", "describe", "assign", "delete"].index
    )
    write_csv(plan, tmp_path / "plan.csv")
    rows = list(csv.reader((tmp_path / "plan.csv").open(encoding="utf-8")))
    assert ["SERMON CHANGES"] in rows and ["TERMS"] in rows and ["NEEDS A HUMAN"] in rows
    assert [
        "2025-08-03",
        "Rich Toward God",
        "ctc_sermon_book",
        "Luke 12:13-21, Ecclesiastes 1:2, 12-14, 2:18-26",
        "Luke 12:13-21; Ecclesiastes 1:2, 12-14, 2:18-26",
        "100",
    ] in rows


def test_alias_under_two_canonicals_rejected() -> None:
    with pytest.raises(ValueError, match="listed under"):
        Mapping.model_validate({"series": {"canonical": {"A": ["X"], "B": ["x"]}}})
