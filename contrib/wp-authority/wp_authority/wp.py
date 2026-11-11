# SPDX-License-Identifier: AGPL-3.0-only
"""WP-CLI, run locally or over SSH; reads with `--format=json`, writes with `eval-file`."""

import base64
import json
import shlex
import subprocess
from importlib import resources
from typing import Any

TAXONOMIES = ("ctc_sermon_book", "ctc_sermon_series", "ctc_sermon_speaker")
STATUSES = "publish,future,draft,pending,private,trash"
MARKER = "@@WP-AUTHORITY@@"


class WPError(RuntimeError):
    pass


class WP:
    def __init__(self, wp: str = "wp", path: str | None = None, ssh: str | None = None):
        self.base = shlex.split(wp)
        self.path = path
        self.ssh = ssh

    def argv(self, args: list[str]) -> list[str]:
        command = [*self.base, *args, *([f"--path={self.path}"] if self.path else [])]
        return ["ssh", self.ssh, shlex.join(command)] if self.ssh else command

    def run(self, args: list[str], stdin: str | None = None) -> str:
        result = subprocess.run(
            self.argv(args), input=stdin, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise WPError(f"{shlex.join(args[:3])} failed: {result.stderr.strip()[-2000:]}")
        return result.stdout

    def json(self, args: list[str]) -> Any:
        return json.loads(self.run([*args, "--format=json"]))

    def php(self, script: str, payload: Any) -> Any:
        """Run one of the bundled PHP scripts with a JSON payload; returns its JSON result."""
        code = resources.files("wp_authority").joinpath(f"php/{script}").read_text("utf-8")
        data = base64.b64encode(json.dumps(payload).encode()).decode()
        code = code.replace("'@PAYLOAD@'", f"'{data}'")
        out = self.run(["eval-file", "-"], stdin=code)
        for line in reversed(out.splitlines()):
            if line.startswith(MARKER):
                return json.loads(line[len(MARKER) :])
        raise WPError(f"no result from {script}: {out[-2000:]}")

    def snapshot(self) -> dict[str, Any]:
        """All terms of the three taxonomies and every sermon's assignments to them."""
        fields = "--fields=term_id,name,slug,parent,description,count"
        terms = {t: self.json(["term", "list", t, fields]) for t in TAXONOMIES}
        posts = self.json(
            [
                "post",
                "list",
                "--post_type=ctc_sermon",
                f"--post_status={STATUSES}",
                "--posts_per_page=-1",
                "--orderby=ID",
                "--order=ASC",
                "--fields=ID,post_date,post_title,post_status",
            ]
        )
        assigned = self.php("assignments.php", {"taxonomies": TAXONOMIES})
        sermons = [
            {
                "id": int(post["ID"]),
                "date": post["post_date"],
                "title": post["post_title"],
                "status": post["post_status"],
                "terms": {t: assigned.get(str(post["ID"]), {}).get(t, []) for t in TAXONOMIES},
            }
            for post in posts
        ]
        return {
            "terms": {
                t: [
                    {
                        "id": int(term["term_id"]),
                        "name": term["name"],
                        "slug": term["slug"],
                        "parent": int(term["parent"]),
                        "description": term.get("description", ""),
                    }
                    for term in rows
                ]
                for t, rows in terms.items()
            },
            "sermons": sermons,
        }
