# SPDX-License-Identifier: AGPL-3.0-only
import shutil
import subprocess
from pathlib import Path

import pytest

from georg.core.config import default_renditions
from georg.core.types import TimeRange
from georg.media import (
    MediaError,
    artifact_ref,
    cut,
    encode_mp3,
    extract_audio,
    probe,
    verify_decode,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def tone_video(path: Path, seconds: float, freq: int = 440) -> Path:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
         "-f", "lavfi", "-i", f"color=size=64x64:rate=10:duration={seconds}",
         "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path)],
        check=True,
    )  # fmt: skip
    return path


def test_probe_reports_streams(tmp_path: Path) -> None:
    info = probe(tone_video(tmp_path / "a.mp4", 2))
    assert info.has_audio and info.has_video
    assert abs(info.duration_ms - 2000) < 150


def test_extract_joins_parts_in_order(tmp_path: Path) -> None:
    parts = [tone_video(tmp_path / "1.mp4", 2), tone_video(tmp_path / "2.mp4", 3, 880)]
    out = extract_audio(parts, tmp_path / "service.flac")
    info = probe(out)
    assert not info.has_video
    assert info.sample_rate == 44100 and info.channels == 2
    assert abs(info.duration_ms - 5000) < 200


def test_cut_and_encode_renditions(tmp_path: Path) -> None:
    service = extract_audio([tone_video(tmp_path / "v.mp4", 6)], tmp_path / "s.flac")
    sermon = cut(service, TimeRange(start_ms=1000, end_ms=4000), tmp_path / "c.flac", fade_ms=200)
    assert abs(probe(sermon).duration_ms - 3000) < 100
    for spec in default_renditions():
        mp3 = encode_mp3(sermon, tmp_path / f"{spec.name}.mp3", spec, tags={"title": "Test"})
        ref = artifact_ref(mp3, "audio/mpeg", expected_ms=3000)
        info = probe(mp3)
        assert info.channels == spec.channels and info.sample_rate == spec.sample_rate
        assert len(ref.sha256) == 64 and ref.size > 0
    assert not list(tmp_path.glob(".*partial*"))


def test_verify_rejects_wrong_duration_and_garbage(tmp_path: Path) -> None:
    flac = extract_audio([tone_video(tmp_path / "v.mp4", 2)], tmp_path / "s.flac")
    with pytest.raises(MediaError, match="expected"):
        verify_decode(flac, expected_ms=5000)
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio" * 100)
    with pytest.raises(MediaError):
        verify_decode(junk)
