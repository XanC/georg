# SPDX-License-Identifier: AGPL-3.0-only
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from georg.core.config import RenditionSpec, default_renditions
from georg.core.types import ServiceBoundaries
from georg.media import MediaError, probe
from georg.media.mastering import MasteringParams, master, normalize_gain, render

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def speechlike(path: Path, seconds: float, *, level_db: float, channels: int = 2) -> Path:
    """Pink noise with a slow, deep level swing, like a voice moving off the microphone."""
    layout = "stereo" if channels == 2 else "mono"
    source = (
        f"anoisesrc=color=pink:sample_rate=44100:amplitude=0.5:duration={seconds},"
        f"volume='0.1+0.9*abs(sin(0.7*t))':eval=frame,volume={level_db}dB,"
        f"aformat=sample_fmts=s16:channel_layouts={layout}"
    )
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, "-c:a", "flac",
         str(path)],
        check=True,
    )  # fmt: skip
    return path


def integrated_lufs(path: Path) -> float:
    """Independent EBU R128 measurement with the `ebur128` filter."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null",
         "-"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    summary = proc.stderr[proc.stderr.rindex("Summary:") :]
    match = re.search(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", summary)
    assert match, summary
    return float(match.group(1))


@pytest.mark.parametrize("level_db", [-30.0, -3.0])
def test_master_hits_loudness_target(tmp_path: Path, level_db: float) -> None:
    src = speechlike(tmp_path / "in.flac", 20, level_db=level_db)
    out = master(src, tmp_path / "out.flac")
    assert abs(integrated_lufs(out) - (-16.0)) <= 1.0
    info, src_info = probe(out), probe(src)
    assert info.sample_rate == 44100 and info.channels == 2
    assert abs(info.duration_ms - src_info.duration_ms) <= 50
    assert not list(tmp_path.glob(".*partial*"))


def test_master_custom_target_without_highpass(tmp_path: Path) -> None:
    src = speechlike(tmp_path / "in.flac", 15, level_db=-12, channels=1)
    params = MasteringParams(highpass_hz=None, ratio=6, target_lufs=-20, true_peak_db=-2)
    out = master(src, tmp_path / "out.flac", params)
    assert abs(integrated_lufs(out) - (-20.0)) <= 1.0
    assert probe(out).channels == 1


def test_master_rejects_silence(tmp_path: Path) -> None:
    src = tmp_path / "silence.flac"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "anullsrc=r=44100:cl=stereo", "-t", "5", "-c:a", "flac", str(src)],
        check=True,
    )  # fmt: skip
    with pytest.raises(MediaError, match="loudness"):
        master(src, tmp_path / "out.flac")


def test_normalize_gain_is_peak_safe(tmp_path: Path) -> None:
    src = speechlike(tmp_path / "in.flac", 15, level_db=-30)
    out = normalize_gain(src, tmp_path / "out.flac")
    measured = integrated_lufs(out)
    assert measured > integrated_lufs(src) + 5
    assert measured <= -16.0 + 1.0


def test_render_cuts_and_encodes_each_spec(tmp_path: Path) -> None:
    mastered = master(speechlike(tmp_path / "in.flac", 12, level_db=-20), tmp_path / "m.flac")
    # The mastered file holds the service span, which starts 5 s into the source timeline.
    b = ServiceBoundaries(
        service_start_ms=5000, sermon_start_ms=8000, sermon_end_ms=13000, service_end_ms=17000
    )
    specs = default_renditions()
    refs = render(mastered, b, specs, tmp_path / "out", origin_ms=5000, tags={"title": "T"})
    assert set(refs) == {s.name for s in specs}
    expected = {"service": 12000, "sermon": 5000}
    for spec in specs:
        ref = refs[spec.name]
        info = probe(ref.path)
        assert ref.path == tmp_path / "out" / f"{spec.name}.mp3"
        assert ref.mime == "audio/mpeg" and ref.size == ref.path.stat().st_size
        assert info.channels == spec.channels and info.sample_rate == spec.sample_rate
        assert ref.duration_ms is not None
        assert abs(ref.duration_ms - expected[spec.span]) <= 250
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(
        f"{s.name}.mp3" for s in specs
    )


def test_render_rejects_span_outside_mastered_audio(tmp_path: Path) -> None:
    mastered = speechlike(tmp_path / "m.flac", 4, level_db=-20)
    b = ServiceBoundaries(
        service_start_ms=0, sermon_start_ms=1000, sermon_end_ms=2000, service_end_ms=9000
    )
    spec = RenditionSpec(name="service", span="service", bitrate_kbps=32)
    with pytest.raises(MediaError, match="outside"):
        render(mastered, b, [spec], tmp_path / "out")
