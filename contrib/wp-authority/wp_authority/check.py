# SPDX-License-Identifier: AGPL-3.0-only
"""Check the calendar against sermons that already have a series."""

from datetime import date
from typing import Any

from wp_authority.liturgical import church_year, days
from wp_authority.mapping import Mapping
from wp_authority.names import series_key
from wp_authority.scripture import clean_name
from wp_authority.wp import TAXONOMIES

SERIES = TAXONOMIES[1]


def check_calendar(
    snapshot: dict[str, Any], mapping: Mapping, skip_years: set[int]
) -> dict[str, Any]:
    """Compare each dated series with the computed days for its date.

    A sermon agrees when its series (through the canonical table) has the same
    `series_key` as one of the day's names. Sermons in `skip_years` (church years, named
    by the year they end in, e.g. one that used the one-year lectionary) are skipped.
    """
    names = {t["id"]: clean_name(t["name"]) for t in snapshot["terms"][SERIES]}
    agree: list[dict[str, Any]] = []
    disagree: list[dict[str, Any]] = []
    for sermon in snapshot["sermons"]:
        day = date.fromisoformat(sermon["date"][:10])
        ids = [i for i in sermon["terms"].get(SERIES, []) if i in names]
        if not ids or church_year(day) in skip_years or sermon["status"] == "trash":
            continue
        found = days(day)
        computed = {series_key(mapping.series.resolve(n)) for d in found for n in d.names}
        series = [mapping.series.resolve(names[i]) for i in ids]
        row = {
            "post": sermon["id"],
            "date": sermon["date"][:10],
            "weekday": day.strftime("%a"),
            "series": series,
            "computed": [d.name for d in found],
        }
        if any(series_key(s) in computed for s in series):
            agree.append(row)
        else:
            disagree.append(row)
    total = len(agree) + len(disagree)
    sundays = [r for r in agree + disagree if r["weekday"] == "Sun"]
    sundays_agree = [r for r in agree if r["weekday"] == "Sun"]
    return {
        "checked": total,
        "agree": len(agree),
        "rate": round(len(agree) / total, 4) if total else None,
        "sundays_checked": len(sundays),
        "sundays_agree": len(sundays_agree),
        "sundays_rate": round(len(sundays_agree) / len(sundays), 4) if sundays else None,
        "disagree": disagree,
    }
