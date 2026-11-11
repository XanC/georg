# SPDX-License-Identifier: AGPL-3.0-only
"""Apply + restore round trip against a throwaway WordPress (scripts/wordpress-e2e).

Skipped unless WP_AUTHORITY_WP is the WP-CLI command for that site, e.g.
`docker compose -f scripts/wordpress-e2e/compose.yaml --progress quiet run --rm -T cli wp`;
set WP_AUTHORITY_URL (e.g. http://localhost:8080) to also check the person's archive page.
"""

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from wp_authority.__main__ import main
from wp_authority.mapping import Mapping
from wp_authority.plan import build_plan
from wp_authority.wp import WP

COMMAND = os.environ.get("WP_AUTHORITY_WP")
pytestmark = pytest.mark.skipif(not COMMAND, reason="set WP_AUTHORITY_WP to a test site's WP-CLI")


FIXTURES = Path(__file__).parent / "fixtures"
SEED = (FIXTURES / "seed.php").read_text("utf-8")
ARCHIVE = (FIXTURES / "archive.php").read_text("utf-8")


def php(wp: WP, code: str) -> Any:
    out = wp.run(["eval-file", "-"], stdin=code)
    return json.loads(out.rsplit("@@WP-AUTHORITY@@", 1)[1])


def comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "terms": {t: sorted(rows, key=lambda r: r["id"]) for t, rows in snapshot["terms"].items()},
        "sermons": sorted(
            (
                {"id": s["id"], "terms": {t: sorted(i) for t, i in s["terms"].items()}}
                for s in snapshot["sermons"]
            ),
            key=lambda s: s["id"],
        ),
    }


def test_apply_restore_round_trip(tmp_path: Path) -> None:
    assert COMMAND
    wp = WP(COMMAND)
    posts = php(wp, SEED)
    try:
        before = wp.snapshot()
        mapping = Mapping.model_validate(
            {
                "series": {"canonical": {"All Saints' Day": ["All Saints Sunday", "All Saints"]}},
                "speakers": {"people": {"Ross Davis": ["Vicar Ross Davis", "Rev. Ross Davis"]}},
                "unused": {"delete": True},
            }
        )
        plan = build_plan(before, mapping)
        (tmp_path / "plan.json").write_text(json.dumps(plan), "utf-8")
        args = ["--wp", COMMAND]
        assert (
            main([*args, "apply", str(tmp_path / "plan.json"), "--backup-dir", str(tmp_path)]) == 0
        )

        after = wp.snapshot()
        by_id = {s["id"]: s for s in after["sermons"]}
        names = {t: {r["id"]: r for r in rows} for t, rows in after["terms"].items()}
        books = [
            names["ctc_sermon_book"][i]["name"] for i in by_id[posts[0]]["terms"]["ctc_sermon_book"]
        ]
        assert sorted(books) == ["Ecclesiastes 1:2, 12-14, 2:18-26", "Luke 12:13-21"]
        (child,) = by_id[posts[2]]["terms"]["ctc_sermon_book"]
        assert names["ctc_sermon_book"][child]["name"] == "Mark 10:2-16"
        original = {r["name"]: r for r in before["terms"]["ctc_sermon_book"]}
        assert names["ctc_sermon_book"][child]["slug"] == original["Mark 10: 2-16"]["slug"]
        series = {
            names["ctc_sermon_series"][i]["name"]
            for p in posts[:2]
            for i in by_id[p]["terms"]["ctc_sermon_series"]
        }
        assert series == {"All Saints' Day"}
        speakers = names["ctc_sermon_speaker"]
        (parent,) = [r for r in speakers.values() if r["name"] == "Ross Davis"]
        assert {r["name"] for r in speakers.values() if r["parent"] == parent["id"]} == {
            "Vicar Ross Davis",
            "Rev. Ross Davis",
        }
        assert "wpa Unused Speaker" not in {r["name"] for r in speakers.values()}
        titles = php(wp, ARCHIVE)
        assert {"wpa Rich Toward God", "wpa Opened", "wpa Children"} <= set(titles)
        if url := os.environ.get("WP_AUTHORITY_URL"):
            page = httpx.get(f"{url}/?ctc_sermon_speaker={parent['slug']}", follow_redirects=True)
            for title in ("wpa Rich Toward God", "wpa Opened", "wpa Children"):
                assert title in page.text

        assert (
            main(
                [
                    *args,
                    "apply",
                    str(tmp_path / "plan.json"),
                    "--backup-dir",
                    str(tmp_path / "again"),
                ]
            )
            == 0
        )
        assert comparable(wp.snapshot()) == comparable(after)

        (backup,) = tmp_path.glob("backup-*.json")
        assert main([*args, "restore", str(backup)]) == 0
        assert comparable(wp.snapshot()) == comparable(before)
    finally:
        php(
            wp,
            "<?php foreach ( "
            + json.dumps(posts).replace("[", "array(").replace("]", ")")
            + ' as $id ) { wp_delete_post( $id, true ); } echo "\\n@@WP-AUTHORITY@@null\\n";',
        )
