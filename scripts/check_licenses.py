# SPDX-License-Identifier: AGPL-3.0-only
"""Fail if an installed runtime dependency has a license outside the allow-list.

Run with: `uv run --no-dev --with pip-licenses python scripts/check_licenses.py`
(add `--all-extras` to check optional extras too).
"""

import json
import subprocess
import sys

ALLOWED_SUBSTRINGS = (
    "MIT",
    "BSD",
    "Apache",
    "ISC",
    "PSF",
    "Python Software Foundation",
    "MPL-2.0",
    "Mozilla Public License 2.0",
    "LGPL",
    "GPL-3.0",
    "GPLv3",
    "AGPL-3.0",
    "Unlicense",
    "The Unlicense",
    "CC0",
    "Zlib",
)
IGNORED_PACKAGES = {"georg", "pip-licenses", "prettytable", "wcwidth", "tomli"}
# Packages whose metadata omits the license; verified by reading their LICENSE files.
KNOWN_LICENSES = {
    "namex": "Apache-2.0",
    "pyannote-core": "MIT",
}


def main() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--from=mixed"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    bad = [
        pkg
        for pkg in json.loads(out)
        if pkg["Name"].lower() not in IGNORED_PACKAGES
        and not any(
            s in KNOWN_LICENSES.get(pkg["Name"].lower(), pkg["License"]) for s in ALLOWED_SUBSTRINGS
        )
    ]
    for pkg in bad:
        print(f"disallowed or unknown license: {pkg['Name']} {pkg['Version']}: {pkg['License']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
