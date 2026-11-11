# SPDX-License-Identifier: AGPL-3.0-only
"""Regenerate wp_authority/data/lectionary.json from The Wartburg Project's LSB pages.

Only the reading references are kept (no Bible text). Run from contrib/wp-authority:

    uv run --project ../.. python tools/build_lectionary.py [--cache DIR]
"""

import argparse
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wp_authority.scripture import Unparseable, split_references

BASE = "https://wartburgproject.org/resources/lectionaries/lsb"
SEASONS_3 = ("advent", "christmas", "epiphany", "lent", "easter", "pentecost")
MONTHS = "January February March April May June July August September October November December"
_MONTH = {name: i + 1 for i, name in enumerate(MONTHS.split())}
OUT = Path(__file__).resolve().parents[1] / "wp_authority" / "data" / "lectionary.json"


def fetch(path: str, cache: Path | None) -> str:
    if cache is not None:
        local = cache / (path.replace("/", "-") + ".html")
        if local.exists():
            return local.read_text(encoding="utf-8")
    request = urllib.request.Request(f"{BASE}/{path}", headers={"User-Agent": "curl/8.5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        text: str = response.read().decode("utf-8")
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        local.write_text(text, encoding="utf-8")
    return text


def headings(page: str) -> list[tuple[int, str]]:
    found = []
    for match in re.finditer(r"<h([234])\b[^>]*>(.*?)</h\1>", page, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", match.group(2)))
        found.append((int(match.group(1)), " ".join(text.split())))
    return found


def day_key(name: str) -> str | None:
    """The calendar's name for a Wartburg heading (several headings can share one)."""
    rules = (
        ("Christmas Eve", "Christmas Eve"),
        ("Nativity of Our Lord", "Christmas Day"),
        ("Resurrection of Our Lord", "The Resurrection of Our Lord"),
        ("Baptism of our Lord", "The Baptism of Our Lord"),
        ("Transfiguration", "The Transfiguration of Our Lord"),
        ("Epiphany of", "The Epiphany of Our Lord"),
        ("Palm Sunday", "Palm Sunday"),
        ("Day of Pentecost", "The Day of Pentecost"),
        ("Holy Trinity", "The Holy Trinity"),
        ("Ascension", "The Ascension of Our Lord"),
    )
    for needle, key in rules:
        if needle.lower() in name.lower():
            return key
    if name in ("or", "Lutheran Service Book", "Connect with Us!") or "Lectionary" in name:
        return None
    name = name.removeprefix("The ").removesuffix(" Sunday")
    return name.replace("\u2019", "'")


def readings(text: str) -> list[str]:
    text = re.sub(r":\s*\(", ":", text)
    text = re.sub(r"\((\d[^)]*)\)\s*(?=\d)", r", \1, ", text)
    text = text.replace("(", ", ").replace(")", ", ")
    text = re.sub(r"(,\s*)+", ", ", text).strip(" ,")
    text = re.sub(r"([A-Za-z]{2,})\s*,\s*(?=\d)", r"\1 ", text)
    return split_references(text)


def season_table(pages: list[str]) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for page in pages:
        key: str | None = None
        for level, text in headings(page):
            if level == 2:
                key = day_key(text)
                if key is not None:
                    table.setdefault(key, [])
            elif level == 4 and key is not None:
                add(table[key], text, key)
    return {k: v for k, v in table.items() if v}


def add(into: list[str], text: str, where: str) -> None:
    try:
        refs = readings(text)
    except Unparseable:
        print(f"skipped {text!r} ({where})", file=sys.stderr)
        return
    into.extend(ref for ref in refs if ref not in into)


def feasts(page: str) -> dict[str, dict[str, object]]:
    table: dict[str, dict[str, object]] = {}
    key: str | None = None
    for level, text in headings(page):
        if level == 2:
            dated = re.match(rf"^(.*?)\s*\(({'|'.join(_MONTH)}) (\d+)\)$", text)
            if text.startswith("(") and key is not None:
                dated = re.match(rf"^\(({'|'.join(_MONTH)}) (\d+)\)$", text)
                if dated:
                    table[key].update(month=_MONTH[dated[1]], day=int(dated[2]))
                continue
            name = dated[1] if dated else text
            key = day_key(name.rstrip(","))
            if key is None:
                continue
            entry: dict[str, object] = {"readings": []}
            if dated:
                entry.update(month=_MONTH[dated[2]], day=int(dated[3]))
            table[key] = entry
        elif level == 4 and key is not None:
            refs = table[key]["readings"]
            assert isinstance(refs, list)
            add(refs, text, key)
    return {k: v for k, v in table.items() if v["readings"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    tables: dict[str, object] = {}
    for year in "abc":
        pages = [fetch(f"3year-{year}/{s}", args.cache) for s in SEASONS_3]
        tables[f"lsb-3{year}"] = season_table(pages)
    tables["lsb-feasts"] = feasts(fetch("occasions", args.cache))
    data = {
        "source": f"{BASE} (Lutheran Service Book lectionaries, references only)",
        "tables": tables,
    }
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
