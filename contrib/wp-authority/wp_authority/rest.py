# SPDX-License-Identifier: AGPL-3.0-only
"""A read-only snapshot from the public REST API, for planning before WP-CLI is available.

It sees published sermons only, and only the terms they use plus whatever the term
listings return, so `apply` still needs a plan made from a WP-CLI `dump`.
"""

from collections.abc import Iterable
from typing import Any

import httpx

from wp_authority.wp import TAXONOMIES

USER_AGENT = "curl/8.5.0"
TERM_FIELDS = "id,name,slug,parent,description"
SERMON_FIELDS = "id,date,title," + ",".join(TAXONOMIES)


class RestSnapshot:
    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/wp-json/wp/v2/",
            headers={"User-Agent": USER_AGENT},
            transport=transport,
            timeout=60,
            follow_redirects=True,
        )
        self.warnings: list[str] = []

    def _pages(self, path: str, fields: str) -> dict[int, dict[str, Any]]:
        found: dict[int, dict[str, Any]] = {}
        page, total = 1, 1
        reported = 0
        while page <= total:
            response = self.client.get(
                path,
                params={
                    "per_page": 100,
                    "page": page,
                    "orderby": "id",
                    "order": "asc",
                    "_fields": fields,
                },
            )
            response.raise_for_status()
            total = int(response.headers.get("X-WP-TotalPages", 1))
            reported = int(response.headers.get("X-WP-Total", 0))
            for row in response.json():
                found[int(row["id"])] = row
            page += 1
        if reported and len(found) < reported:
            self.warnings.append(f"{path}: listing returned {len(found)} of {reported}")
        return found

    def _include(self, path: str, ids: Iterable[int]) -> list[dict[str, Any]]:
        wanted = sorted(set(ids))
        rows: list[dict[str, Any]] = []
        for start in range(0, len(wanted), 100):
            batch = wanted[start : start + 100]
            response = self.client.get(
                path,
                params={
                    "include": ",".join(map(str, batch)),
                    "per_page": 100,
                    "_fields": TERM_FIELDS,
                },
            )
            response.raise_for_status()
            rows += response.json()
        return rows

    def snapshot(self) -> dict[str, Any]:
        sermons = self._pages("ctc_sermon", SERMON_FIELDS)
        terms: dict[str, list[dict[str, Any]]] = {}
        for taxonomy in TAXONOMIES:
            found = self._pages(taxonomy, TERM_FIELDS)
            needed = {i for s in sermons.values() for i in s.get(taxonomy, [])}
            while missing := (needed | {t["parent"] for t in found.values() if t["parent"]}) - set(
                found
            ):
                rows = self._include(taxonomy, missing)
                for row in rows:
                    found[int(row["id"])] = row
                lost = missing - {int(r["id"]) for r in rows}
                if lost:
                    self.warnings.append(f"{taxonomy}: no term for ids {sorted(lost)}")
                    needed -= lost
                    for term in found.values():
                        if term["parent"] in lost:
                            term["parent"] = 0
            terms[taxonomy] = [
                {
                    "id": int(t["id"]),
                    "name": t["name"],
                    "slug": t["slug"],
                    "parent": int(t["parent"]),
                    "description": t.get("description", ""),
                }
                for t in sorted(found.values(), key=lambda t: int(t["id"]))
            ]
        return {
            "source": "rest",
            "terms": terms,
            "sermons": [
                {
                    "id": int(s["id"]),
                    "date": s["date"].replace("T", " "),
                    "title": s["title"]["rendered"],
                    "status": "publish",
                    "terms": {t: [int(i) for i in s.get(t, [])] for t in TAXONOMIES},
                }
                for s in sorted(sermons.values(), key=lambda s: int(s["id"]))
            ],
        }
