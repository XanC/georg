# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

import httpx

from wp_authority.rest import RestSnapshot

SERMONS = [
    {
        "id": 2,
        "date": "2025-08-03T10:00:00",
        "title": {"rendered": "Rich &#8217;Toward"},
        "ctc_sermon_book": [10],
        "ctc_sermon_series": [20, 21],
        "ctc_sermon_speaker": [30],
    },
    {
        "id": 1,
        "date": "2025-07-27T10:00:00",
        "title": {"rendered": "Pray"},
        "ctc_sermon_book": [11],
        "ctc_sermon_series": [],
        "ctc_sermon_speaker": [30],
    },
]
TERMS = {
    "ctc_sermon_book": [{"id": 10, "name": "Luke 12:13-21"}, {"id": 11, "name": "Luke 11:1-13"}],
    "ctc_sermon_series": [
        {"id": 20, "name": "Eighth Sunday after Pentecost", "parent": 22},
        {"id": 21, "name": "Hidden"},
        {"id": 22, "name": "Pentecost"},
        {"id": 23, "name": "Unused"},
    ],
    "ctc_sermon_speaker": [{"id": 30, "name": "Rev. Ross Davis"}],
}
LISTED = {"ctc_sermon_series": [20, 23, 20]}  # skips 21 and 22, repeats 20


def term(row: dict[str, Any]) -> dict[str, Any]:
    return {"slug": row["name"].lower().replace(" ", "-"), "parent": 0, "description": "", **row}


def handler(seen: list[httpx.Request]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = request.url.path.rsplit("/", 1)[1]
        params = request.url.params
        if name == "ctc_sermon":
            return httpx.Response(200, json=SERMONS, headers={"X-WP-TotalPages": "1"})
        rows = {t["id"]: term(t) for t in TERMS[name]}
        if "include" in params:
            ids = [int(i) for i in params["include"].split(",")]
            return httpx.Response(200, json=[rows[i] for i in ids if i in rows])
        page = int(params["page"])
        listed = [rows[i] for i in LISTED.get(name, sorted(rows))]
        body = listed[:2] if page == 1 else listed[2:]
        headers = {"X-WP-TotalPages": "2", "X-WP-Total": str(len(rows))}
        return httpx.Response(200, json=body, headers=headers)

    return httpx.MockTransport(respond)


def test_dump_rest() -> None:
    seen: list[httpx.Request] = []
    reader = RestSnapshot("https://example.org/", transport=handler(seen))
    snap = reader.snapshot()
    assert all(r.headers["User-Agent"] == "curl/8.5.0" for r in seen)
    assert seen[0].url.path == "/wp-json/wp/v2/ctc_sermon"
    assert [s["id"] for s in snap["sermons"]] == [1, 2]
    assert snap["sermons"][1] == {
        "id": 2,
        "date": "2025-08-03 10:00:00",
        "title": "Rich &#8217;Toward",
        "status": "publish",
        "terms": {
            "ctc_sermon_book": [10],
            "ctc_sermon_series": [20, 21],
            "ctc_sermon_speaker": [30],
        },
    }
    series = {t["id"]: t for t in snap["terms"]["ctc_sermon_series"]}
    assert sorted(series) == [20, 21, 22, 23]
    assert series[20]["parent"] == 22
    includes = [r.url.params["include"] for r in seen if "include" in r.url.params]
    assert includes == ["21,22"]
    assert reader.warnings == ["ctc_sermon_series: listing returned 2 of 4"]
