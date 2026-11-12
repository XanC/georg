# SPDX-License-Identifier: AGPL-3.0-only
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from georg.core.contracts import AudioClassifier
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import SegmentLabel
from georg.modules.processors.classify import YamnetClassifier, YamnetOptions, yamnet

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

CLASSES = [
    "Speech",
    "Chatter",
    "Music",
    "Organ",
    "Hymn",
    "Silence",
    "Dog bark",
    "Wind",
]


def row(**groups: float) -> list[float]:
    """One score row: `groups` maps a group name to a score for its classes."""
    scores = [0.0] * len(CLASSES)
    for group, value in groups.items():
        for i, name in enumerate(CLASSES):
            if name in getattr(yamnet, f"{group.upper()}_CLASSES"):
                scores[i] = value
    return scores


class FakeYamnet:
    """Returns scripted score rows, one list per classify call."""

    def __init__(self, calls: list[list[list[float]]]) -> None:
        self.calls = list(calls)
        self.class_names = list(CLASSES)
        self.sample_lengths: list[int] = []

    def __call__(self, samples: Sequence[float]) -> Sequence[Sequence[float]]:
        self.sample_lengths.append(len(samples))
        if not self.calls:
            raise AssertionError("model called more times than scripted")
        return self.calls.pop(0)


def classifier(fake: FakeYamnet, **options: object) -> tuple[YamnetClassifier, list[YamnetOptions]]:
    calls: list[YamnetOptions] = []
    c = YamnetClassifier(dict(options), model_factory=lambda opts: (calls.append(opts), fake)[1])
    return c, calls


def tone(path: Path, seconds: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-ac", "1", "-ar", "16000",
         "-c:a", "flac", str(path)],
        check=True,
    )  # fmt: skip
    return path


def silent(path: Path, seconds: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anullsrc=r=16000:cl=mono:d={seconds}", "-c:a", "flac", str(path)],
        check=True,
    )  # fmt: skip
    return path


def spans(c: YamnetClassifier, audio: Path) -> list[tuple[SegmentLabel, int, int]]:
    return [(s.label, s.range.start_ms, s.range.end_ms) for s in c.classify(audio)]


def test_satisfies_protocol() -> None:
    c = YamnetClassifier({"chunk_s": 120, "min_segment_s": 2.0})
    assert isinstance(c, AudioClassifier)
    assert c.type_name == "yamnet"
    assert "c120" in c.version and "m2.0" in c.version
    assert YamnetClassifier({"smooth_window_s": 5.0}).version != c.version


def test_maps_audioset_groups(tmp_path: Path) -> None:
    rows = (
        [row(music=0.9)] * 10
        + [row(speech=0.9)] * 20  # Speech and Chatter both score the speech group
        + [row(silence=0.9)] * 5
        + [row()] * 5
    )
    for r in rows[35:]:
        r[CLASSES.index("Dog bark")] = 0.9  # not in any group -> noise
    c, _ = classifier(
        FakeYamnet([rows]), smooth_window_s=0.5, min_group_score=0.5, min_segment_s=1.0
    )
    assert spans(c, tone(tmp_path / "a.flac", 20)) == [
        ("music", 0, 4800),
        ("speech", 4800, 14400),
        ("silence", 14400, 16800),
        ("noise", 16800, 20000),
    ]


def test_organ_and_hymn_count_as_music(tmp_path: Path) -> None:
    scores = [0.0] * len(CLASSES)
    scores[CLASSES.index("Organ")] = 0.4
    scores[CLASSES.index("Hymn")] = 0.4
    scores[CLASSES.index("Dog bark")] = 0.5
    c, _ = classifier(FakeYamnet([[scores] * 5]), smooth_window_s=0.5)
    assert spans(c, tone(tmp_path / "a.flac", 3)) == [("music", 0, 3000)]


def test_smoothing_suppresses_single_frame_flicker(tmp_path: Path) -> None:
    rows = [row(music=0.9)] * 20
    rows[10] = row(speech=0.9)
    c, _ = classifier(FakeYamnet([rows]), smooth_window_s=2.4, min_segment_s=4.0)
    assert spans(c, tone(tmp_path / "a.flac", 11)) == [("music", 0, 11000)]


def test_min_segment_merges_short_island(tmp_path: Path) -> None:
    rows = [row(music=0.9)] * 30
    rows[10:12] = [row(speech=0.9)] * 2  # 0.96 s speech island
    c, _ = classifier(FakeYamnet([rows]), smooth_window_s=0.5, min_segment_s=4.0)
    assert spans(c, tone(tmp_path / "a.flac", 15)) == [("music", 0, 15000)]


def test_min_segment_disabled_keeps_island(tmp_path: Path) -> None:
    rows = [row(music=0.9)] * 30
    rows[10:12] = [row(speech=0.9)] * 2
    c, _ = classifier(FakeYamnet([rows]), smooth_window_s=0.5, min_segment_s=0.0)
    labels = [label for label, _, _ in spans(c, tone(tmp_path / "a.flac", 15))]
    assert labels == ["music", "speech", "music"]


def test_low_rms_is_silence(tmp_path: Path) -> None:
    c, _ = classifier(FakeYamnet([[row(music=0.9)] * 10]), smooth_window_s=0.5)
    assert spans(c, silent(tmp_path / "a.flac", 5)) == [("silence", 0, 5000)]


def test_chunks_stitch_at_boundaries(tmp_path: Path) -> None:
    keep = 60_000 // yamnet.HOP_MS  # frames kept from the first chunk
    calls = [[row(music=0.9)] * (keep + 4), [row(speech=0.9)] * 10]
    c, _ = classifier(FakeYamnet(calls), chunk_s=60, overlap_s=2.0, smooth_window_s=0.5)
    result = spans(c, tone(tmp_path / "a.flac", 65))
    assert result[0] == ("music", 0, 60_000)
    assert result[1][0] == "speech"
    assert result[-1][2] == 65_000


def test_release_reloads_the_model(tmp_path: Path) -> None:
    fake = FakeYamnet([[row(music=0.9)] * 5, [row(music=0.9)] * 5])
    c, calls = classifier(fake)
    c.classify(tone(tmp_path / "a.flac", 3))
    c.release()
    c.classify(tone(tmp_path / "a.flac", 3))
    assert len(calls) == 2


def test_missing_tensorflow_is_a_module_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tensorflow", None)
    c = YamnetClassifier()
    with pytest.raises(ModuleError, match="classify"):
        c.classify(tone(tmp_path / "a.flac", 3))


def test_invalid_options() -> None:
    with pytest.raises(ConfigError):
        YamnetClassifier({"no_such_option": 1})
    with pytest.raises(ConfigError):
        YamnetClassifier({"chunk_s": 10})
