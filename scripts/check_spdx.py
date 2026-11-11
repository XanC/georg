# SPDX-License-Identifier: AGPL-3.0-only
"""Fail if a Python or PHP source file lacks the project's SPDX identifier line."""

import sys
from pathlib import Path

HEADER = "# SPDX-License-Identifier: AGPL-3.0-only"
PHP_HEADER = "// SPDX-License-Identifier: AGPL-3.0-only"
ROOTS = ("src", "tests", "scripts", "deploy", "contrib")
PHP_ROOTS = ("wordpress", "contrib")


def missing_header(root: Path) -> list[Path]:
    missing = []
    for top in ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            head = path.read_text(encoding="utf-8").splitlines()[:3]
            if HEADER not in head:
                missing.append(path)
    for top in PHP_ROOTS:
        for path in sorted((root / top).rglob("*.php")):
            head = path.read_text(encoding="utf-8").splitlines()[:3]
            if PHP_HEADER not in head:
                missing.append(path)
    return missing


def main() -> int:
    missing = missing_header(Path.cwd())
    for path in missing:
        print(f"missing SPDX header: {path}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
