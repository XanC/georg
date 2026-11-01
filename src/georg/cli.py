# SPDX-License-Identifier: AGPL-3.0-only
import argparse
from collections.abc import Sequence

from georg import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="georg", description=__doc__)
    parser.add_argument("--version", action="version", version=f"georg {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
