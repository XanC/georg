# SPDX-License-Identifier: AGPL-3.0-only
"""wp-authority: plan, apply and restore taxonomy clean-ups through WP-CLI."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from wp_authority.check import check_calendar
from wp_authority.mapping import Mapping
from wp_authority.plan import build_plan, write_csv
from wp_authority.wp import WP


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wp_authority", description=__doc__)
    parser.add_argument("--wp", default="wp", help='WP-CLI command (default "wp")')
    parser.add_argument("--path", help="WordPress directory, passed as --path")
    parser.add_argument("--ssh", help="run WP-CLI on this host over ssh")
    sub = parser.add_subparsers(dest="command", required=True)
    dump = sub.add_parser("dump", help="write a snapshot (the same data a backup holds)")
    dump.add_argument("out", type=Path)
    plan = sub.add_parser("plan", help="write plan.json and plan.csv")
    plan.add_argument("--mapping", type=Path)
    plan.add_argument("--snapshot", type=Path, help="plan from a snapshot file, not the site")
    plan.add_argument("--out-dir", type=Path, default=Path())
    apply = sub.add_parser("apply", help="back up the site, then apply a plan")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--backup-dir", type=Path, default=Path())
    check = sub.add_parser("check-calendar", help="compare the calendar with dated series")
    check.add_argument("--mapping", type=Path)
    check.add_argument("--snapshot", type=Path)
    check.add_argument(
        "--skip-church-year", type=int, action="append", default=[], dest="skip_year"
    )
    check.add_argument("--out", type=Path, default=Path("calendar-check.json"))
    restore = sub.add_parser("restore", help="put a backup back")
    restore.add_argument("backup", type=Path)
    args = parser.parse_args(argv)
    wp = WP(args.wp, args.path, args.ssh)

    if args.command == "dump":
        _write(args.out, wp.snapshot())
    elif args.command == "plan":
        mapping = Mapping.load(args.mapping)
        source = args.snapshot
        snapshot = json.loads(source.read_text("utf-8")) if source else wp.snapshot()
        result = build_plan(snapshot, mapping)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        _write(args.out_dir / "plan.json", result)
        write_csv(result, args.out_dir / "plan.csv")
        print(
            f"{len(result['sermon_changes'])} sermon changes, {len(result['terms'])} term "
            f"actions, {len(result['needs_human'])} need a human, "
            f"{len(result['series_fill'])} proposed series"
        )
    elif args.command == "apply":
        planned = json.loads(args.plan.read_text("utf-8"))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = args.backup_dir / f"backup-{stamp}.json"
        args.backup_dir.mkdir(parents=True, exist_ok=True)
        _write(backup, {"taken": stamp, **wp.snapshot()})
        print(f"backup written to {backup}")
        report = wp.php("apply.php", {"actions": planned["actions"]})
        _write(args.backup_dir / f"apply-{stamp}.json", report)
        results = [r["result"] for r in report["results"]]
        summary = {k: results.count(k) for k in sorted(set(results))}
        print(f"applied: {summary}")
        if any(r.startswith("error") for r in results):
            return 1
    elif args.command == "check-calendar":
        source = args.snapshot
        snapshot = json.loads(source.read_text("utf-8")) if source else wp.snapshot()
        result = check_calendar(snapshot, Mapping.load(args.mapping), set(args.skip_year))
        _write(args.out, result)
        print(
            f"{result['agree']} of {result['checked']} agree ({result['rate']}); Sundays: "
            f"{result['sundays_agree']} of {result['sundays_checked']} ({result['sundays_rate']})"
        )
    else:
        report = wp.php("restore.php", json.loads(args.backup.read_text("utf-8")))
        print(json.dumps(report, indent=1))
        if report["errors"]:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
