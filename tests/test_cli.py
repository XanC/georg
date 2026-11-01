# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import shutil
import subprocess
from pathlib import Path

import pytest

from georg import __version__
from georg.cli import main, parse_timestamp
from georg.media import probe


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_succeeds() -> None:
    assert main([]) == 0


@pytest.mark.parametrize(
    ("text", "ms"),
    [("90", 90_000), ("1:30.5", 90_500), ("01:02:03.250", 3_723_250), ("0:00:00", 0)],
)
def test_parse_timestamp(text: str, ms: int) -> None:
    assert parse_timestamp(text) == ms


def test_parse_timestamp_rejects_garbage() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_timestamp("1:2:3:4")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_master_sample_writes_ab_pair(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "service.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.05:duration=8",
         "-f", "lavfi", "-i", "color=size=64x64:rate=10:duration=8",
         "-shortest", "-c:v", "libx264", "-c:a", "aac", str(src)],
        check=True,
    )  # fmt: skip
    out = tmp_path / "ab"
    assert main(["master-sample", str(src), str(out), "--start", "0:01", "--end", "0:06"]) == 0
    printed = capsys.readouterr().out.split()
    assert printed == [str(out / "A_unprocessed.mp3"), str(out / "B_mastered.mp3")]
    for name in printed:
        info = probe(Path(name))
        assert abs(info.duration_ms - 5000) <= 250 and info.channels == 2
    assert sorted(p.name for p in out.iterdir()) == ["A_unprocessed.mp3", "B_mastered.mp3"]
