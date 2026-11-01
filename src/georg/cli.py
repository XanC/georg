# SPDX-License-Identifier: AGPL-3.0-only
"""Georg command-line interface."""

import argparse
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from georg import __version__
from georg.core.config import RenditionSpec
from georg.core.errors import GeorgError
from georg.core.types import TimeRange

SAMPLE_MS = 120_000
SAMPLE_SPEC = RenditionSpec(name="sample", span="service", channels=2, vbr_quality=2)
_TIMESTAMP = re.compile(r"^(?:(?:(\d+):)?(\d{1,2}):)?(\d+(?:\.\d+)?)$")


def parse_timestamp(value: str) -> int:
    """Parse "SS[.mmm]", "MM:SS[.mmm]" or "HH:MM:SS[.mmm]" into milliseconds."""
    match = _TIMESTAMP.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"invalid timestamp {value!r}")
    hours, minutes, seconds = match.groups()
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)
    return round(total * 1000)


def master_sample(src: Path, out_dir: Path, span: TimeRange) -> tuple[Path, Path]:
    """Write an A/B pair for `span` of `src`: gain-only (A) and the mastering chain (B)."""
    from georg.media import cut, encode_mp3, extract_audio
    from georg.media.mastering import master, normalize_gain

    out_dir.mkdir(parents=True, exist_ok=True)
    a_path, b_path = out_dir / "A_unprocessed.mp3", out_dir / "B_mastered.mp3"
    with tempfile.TemporaryDirectory(dir=out_dir) as work:
        tmp = Path(work)
        audio = extract_audio([src], tmp / "source.flac")
        excerpt = cut(audio, span, tmp / "excerpt.flac")
        encode_mp3(normalize_gain(excerpt, tmp / "a.flac"), a_path, SAMPLE_SPEC)
        encode_mp3(master(excerpt, tmp / "b.flac"), b_path, SAMPLE_SPEC)
    return a_path, b_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="georg", description=__doc__)
    parser.add_argument("--version", action="version", version=f"georg {__version__}")
    commands = parser.add_subparsers(dest="command")
    sample = commands.add_parser(
        "master-sample",
        help="write an A/B pair (gain-only vs mastered) for listening comparisons",
        description="A is the excerpt with a single gain toward the loudness target and no "
        "compression (like the legacy wavegain step alone); B is the full mastering chain. "
        "Both are encoded as stereo MP3 VBR V2.",
    )
    sample.add_argument("input", type=Path, help="recording (audio or video)")
    sample.add_argument("out_dir", type=Path)
    sample.add_argument("--start", type=parse_timestamp, required=True, help="e.g. 0:31:20")
    sample.add_argument("--end", type=parse_timestamp, help="default: start + 2 minutes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "master-sample":
        end = args.end if args.end is not None else args.start + SAMPLE_MS
        if end <= args.start:
            parser.error("--end must be after --start")
        try:
            paths = master_sample(
                args.input, args.out_dir, TimeRange(start_ms=args.start, end_ms=end)
            )
        except GeorgError as exc:
            print(f"georg: {exc}", file=sys.stderr)
            return 1
        for path in paths:
            print(path)
    return 0
