# SPDX-License-Identifier: AGPL-3.0-only
"""The shipped lectionary tables (see README for sources)."""

import json
from functools import cache
from importlib import resources
from typing import Any


@cache
def tables() -> dict[str, dict[str, Any]]:
    text = resources.files("wp_authority").joinpath("data/lectionary.json").read_text("utf-8")
    data: dict[str, dict[str, Any]] = json.loads(text)["tables"]
    return data


def readings(table: str, key: str | None) -> list[str]:
    if key is None:
        return []
    for name, entry in tables()[table].items():
        if name.casefold() == key.casefold():
            refs = entry["readings"] if isinstance(entry, dict) else entry
            return list(refs)
    return []


def feasts() -> dict[str, tuple[int, int]]:
    """Feast name -> (month, day), for the feasts with a fixed date."""
    return {
        name: (entry["month"], entry["day"])
        for name, entry in tables()["lsb-feasts"].items()
        if "month" in entry
    }
