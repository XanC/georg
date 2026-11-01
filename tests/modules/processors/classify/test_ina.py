# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

from georg.core.contracts import AudioClassifier
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import SegmentLabel
from georg.modules.processors.classify import InaOptions, InaSpeechSegmenterClassifier

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

Raw = list[tuple[str, float, float]]


class FakeSegmenter:
    def __init__(self, result: Raw) -> None:
        self.result = result
        self.calls: list[tuple[float | None, float | None]] = []

    def __call__(
        self, medianame: str, start_sec: float | None = None, stop_sec: float | None = None
    ) -> Iterable[tuple[str, float, float]]:
        self.calls.append((start_sec, stop_sec))
        lo = start_sec or 0.0
        hi = stop_sec if stop_sec is not None else float("inf")
        return [(lab, max(a, lo), min(b, hi)) for lab, a, b in self.result if b > lo and a < hi]


def classifier(fake: FakeSegmenter, **options: object) -> InaSpeechSegmenterClassifier:
    return InaSpeechSegmenterClassifier(dict(options), segmenter_factory=lambda _opts: fake)


def silent_audio(path: Path, seconds: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anullsrc=r=16000:cl=mono:d={seconds}", "-c:a", "flac", str(path)],
        check=True,
    )  # fmt: skip
    return path


def spans(segments: Iterable[object]) -> list[tuple[SegmentLabel, int, int]]:
    return [(s.label, s.range.start_ms, s.range.end_ms) for s in segments]  # type: ignore[attr-defined]


def test_maps_ina_labels_and_merges(tmp_path: Path) -> None:
    fake = FakeSegmenter(
        [
            ("noEnergy", 0.0, 1.02),
            ("music", 1.02, 3.5),
            ("male", 3.5, 4.0),
            ("female", 4.0, 5.0),
            ("speech", 5.0, 6.0),
            ("noise", 6.0, 7.0),
            ("noEnergy", 7.0, 7.98),
        ]
    )
    c = classifier(fake)
    assert isinstance(c, AudioClassifier)
    segments = c.classify(silent_audio(tmp_path / "a.flac", 8))
    assert spans(segments) == [
        ("silence", 0, 1020),
        ("music", 1020, 3500),
        ("speech", 3500, 6000),
        ("noise", 6000, 7000),
        ("silence", 7000, 8000),
    ]
    assert fake.calls == [(None, None)]


def test_chunks_long_files_and_merges_across_seams(tmp_path: Path) -> None:
    fake = FakeSegmenter([("music", 0.0, 70.0), ("speech", 70.0, 150.0)])
    segments = classifier(fake, chunk_s=60).classify(silent_audio(tmp_path / "a.flac", 150))
    assert fake.calls == [(0.0, 60.0), (60.0, 120.0), (120.0, 150.0)]
    assert spans(segments) == [("music", 0, 70000), ("speech", 70000, 150000)]


def test_unknown_label_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ModuleError, match="unknown label"):
        classifier(FakeSegmenter([("applause", 0.0, 1.0)])).classify(
            silent_audio(tmp_path / "a.flac", 1)
        )


def test_segmenter_failure_is_wrapped(tmp_path: Path) -> None:
    def boom(_opts: InaOptions) -> FakeSegmenter:
        class Broken(FakeSegmenter):
            def __call__(self, *args: object, **kwargs: object) -> Raw:
                raise RuntimeError("decode failed")

        return Broken([])

    c = InaSpeechSegmenterClassifier(segmenter_factory=boom)
    with pytest.raises(ModuleError, match="decode failed"):
        c.classify(silent_audio(tmp_path / "a.flac", 1))


def test_options_are_validated_and_in_version() -> None:
    with pytest.raises(ConfigError):
        InaSpeechSegmenterClassifier({"vad_engine": "x"})
    with pytest.raises(ConfigError):
        InaSpeechSegmenterClassifier({"unknown": 1})
    a = InaSpeechSegmenterClassifier()
    b = InaSpeechSegmenterClassifier({"energy_ratio": 0.05})
    assert a.version != b.version
    assert a.version.startswith("ina_speech_segmenter-")


@pytest.mark.slow
@pytest.mark.skipif(
    importlib.util.find_spec("inaSpeechSegmenter") is None,
    reason="the 'classify' extra is not installed",
)
def test_real_model_on_synthetic_audio(tmp_path: Path) -> None:
    """Runs the real CNNs (downloads weights on first use)."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    audio = tmp_path / "service.flac"
    graph = "[1]aresample=16000,aformat=channel_layouts=mono[s];[0][s]concat=n=2:v=0:a=1"
    words = "Good morning. Today we read from the gospel according to John. " * 3
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=10",
         "-f", "lavfi", "-i", f"flite=text='{words}':voice=kal",
         "-filter_complex", graph,
         "-c:a", "flac", str(audio)],
        check=True,
    )  # fmt: skip
    segments = InaSpeechSegmenterClassifier().classify(audio)
    labels = {s.label for s in segments}
    assert segments[0].label == "silence"
    assert segments[0].range.end_ms >= 9000
    assert "speech" in labels
