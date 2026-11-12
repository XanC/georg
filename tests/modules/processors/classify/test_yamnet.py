# SPDX-License-Identifier: AGPL-3.0-only
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from georg.core.contracts import AudioClassifier
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import SegmentLabel
from georg.modules.processors.classify import (
    InaYamnetClassifier,
    InaYamnetOptions,
    yamnet,
)
from georg.modules.processors.classify.yamnet import YamnetOptions

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

# Fake class map: indices score the groups; names match only where needed.
SPEECH, MUSIC, SILENCE, BARK, CHATTER = 0, 132, 300, 400, 401
CLASS_NAMES = [""] * 521
CLASS_NAMES[SPEECH] = "Speech"
CLASS_NAMES[MUSIC] = "Music"
CLASS_NAMES[SILENCE] = "Silence"
CLASS_NAMES[BARK] = "Dog bark"
CLASS_NAMES[CHATTER] = "Chatter"  # deliberately not in the speech group


def row(scores: dict[int, float]) -> list[float]:
    """One YAMNet score row: class index -> score."""
    out = [0.0] * len(CLASS_NAMES)
    for index, value in scores.items():
        out[index] = value
    return out


class FakeYamnet:
    """Returns scripted score rows, one list per classify call."""

    def __init__(self, calls: list[list[list[float]]]) -> None:
        self.calls = list(calls)
        self.class_names = list(CLASS_NAMES)

    def __call__(self, samples: Sequence[float]) -> Sequence[Sequence[float]]:
        if not self.calls:
            raise AssertionError("model called more times than scripted")
        return self.calls.pop(0)


Raw = list[tuple[str, float, float]]


class FakeSegmenter:
    def __init__(self, result: Raw) -> None:
        self.result = result

    def __call__(
        self, medianame: str, start_sec: float | None = None, stop_sec: float | None = None
    ) -> Iterable[tuple[str, float, float]]:
        lo = start_sec or 0.0
        hi = stop_sec if stop_sec is not None else float("inf")
        return [(lab, max(a, lo), min(b, hi)) for lab, a, b in self.result if b > lo and a < hi]


def classifier(
    ina: FakeSegmenter, model: FakeYamnet, **options: object
) -> tuple[InaYamnetClassifier, list[YamnetOptions]]:
    calls: list[YamnetOptions] = []
    c = InaYamnetClassifier(
        dict(options),
        segmenter_factory=lambda _opts: ina,
        model_factory=lambda opts: (calls.append(opts), model)[1],
    )
    return c, calls


def tone(path: Path, seconds: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-ac", "1", "-ar", "16000",
         "-c:a", "flac", str(path)],
        check=True,
    )  # fmt: skip
    return path


def spans(segments: Iterable[object]) -> list[tuple[SegmentLabel, int, int]]:
    return [(s.label, s.range.start_ms, s.range.end_ms) for s in segments]  # type: ignore[attr-defined]


def test_satisfies_protocol() -> None:
    c = InaYamnetClassifier(
        {"music_fraction": 0.7, "ina": {"vad_engine": "sm"}, "yamnet": {"chunk_s": 120}}
    )
    assert isinstance(c, AudioClassifier)
    assert c.type_name == "ina_yamnet"
    assert "f0.7" in c.version and "ina" in c.version
    assert "c120" in c.version and c.options.yamnet.url_key() in c.version
    assert InaYamnetClassifier().version != c.version


def test_relabels_ina_noise_to_music(tmp_path: Path) -> None:
    ina = FakeSegmenter([("speech", 0.0, 5.0), ("noise", 5.0, 20.0), ("speech", 20.0, 25.0)])
    # 5-20 s -> frames ~10-41; every frame scores Music.
    yam = FakeYamnet([[row({MUSIC: 0.9})] * 60])
    c, _ = classifier(ina, yam, yamnet={"majority_frames": 1})
    result = c.classify(tone(tmp_path / "a.flac", 25))
    assert spans(result) == [
        ("speech", 0, 5000),
        ("music", 5000, 20000),
        ("speech", 20000, 25000),
    ]


def test_noise_scoring_as_speech_stays_noise(tmp_path: Path) -> None:
    """Congregation chatter: ina says noise, YAMNet says speech -> stays noise."""
    ina = FakeSegmenter([("music", 0.0, 5.0), ("noise", 5.0, 15.0)])
    yam = FakeYamnet([[row({SPEECH: 0.9})] * 40])
    c, _ = classifier(ina, yam, yamnet={"majority_frames": 1})
    assert spans(c.classify(tone(tmp_path / "a.flac", 15))) == [
        ("music", 0, 5000),
        ("noise", 5000, 15000),
    ]


def test_music_fraction_threshold(tmp_path: Path) -> None:
    ina = FakeSegmenter([("speech", 0.0, 5.0), ("noise", 5.0, 15.0)])
    # 40% music inside the noise segment.
    frames = [row({BARK: 0.9})] * 40
    for i in range(11, 20):
        frames[i] = row({MUSIC: 0.9})
    below, _ = classifier(ina, FakeYamnet([list(frames)]), yamnet={"majority_frames": 1})
    assert spans(below.classify(tone(tmp_path / "a.flac", 15)))[1][0] == "noise"
    above, _ = classifier(
        ina,
        FakeYamnet([list(frames)]),
        music_fraction=0.4,
        yamnet={"majority_frames": 1},
    )
    assert spans(above.classify(tone(tmp_path / "a.flac", 15)))[1][0] == "music"


def test_no_noise_segments_skips_yamnet(tmp_path: Path) -> None:
    ina = FakeSegmenter([("speech", 0.0, 5.0), ("music", 5.0, 10.0)])
    c, calls = classifier(ina, FakeYamnet([]))
    assert spans(c.classify(tone(tmp_path / "a.flac", 10))) == [
        ("speech", 0, 5000),
        ("music", 5000, 10000),
    ]
    assert calls == []


def test_release_frees_both_models(tmp_path: Path) -> None:
    ina = FakeSegmenter([("noise", 0.0, 10.0)])
    calls = [[row({MUSIC: 0.9})] * 25, [row({MUSIC: 0.9})] * 25]
    c, yam_calls = classifier(ina, FakeYamnet(calls))
    audio = tone(tmp_path / "a.flac", 10)
    c.classify(audio)
    c.release()
    c.classify(audio)
    assert len(yam_calls) == 2


def test_missing_tensorflow_is_a_module_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tensorflow", None)
    c = InaYamnetClassifier(segmenter_factory=lambda _o: FakeSegmenter([("noise", 0.0, 5.0)]))
    with pytest.raises(ModuleError, match="classify"):
        c.classify(tone(tmp_path / "a.flac", 5))


def test_invalid_options() -> None:
    with pytest.raises(ConfigError):
        InaYamnetClassifier({"no_such_option": 1})
    with pytest.raises(ConfigError):
        InaYamnetClassifier({"music_fraction": 1.5})
    opts = InaYamnetOptions.model_validate({"ina": {"chunk_s": 120}})
    assert opts.ina.chunk_s == 120


def test_frame_label_groups() -> None:
    assert yamnet.frame_label(CLASS_NAMES, row({MUSIC: 0.4}), 0.1) == "music"
    assert yamnet.frame_label(CLASS_NAMES, row({SPEECH: 0.4}), 0.1) == "speech"
    assert yamnet.frame_label(CLASS_NAMES, row({SILENCE: 0.4}), 0.1) == "silence"
    # Unmapped classes never win: dominant bark -> noise.
    assert yamnet.frame_label(CLASS_NAMES, row({BARK: 0.9}), 0.1) == "noise"
    # Below the noise threshold even when a group is the argmax.
    assert yamnet.frame_label(CLASS_NAMES, row({MUSIC: 0.05}), 0.1) == "noise"
    # Chatter is not speech.
    assert yamnet.frame_label(CLASS_NAMES, row({CHATTER: 0.9}), 0.1) == "noise"


def test_majority_filter_suppresses_flicker() -> None:
    labels: list[SegmentLabel] = ["music"] * 20
    labels[10] = "speech"
    assert set(yamnet._majority(labels, 5)) == {"music"}
    # A real run survives.
    labels[9:13] = ["speech"] * 4
    out = yamnet._majority(labels, 5)
    assert out[10] == "speech" and out[0] == "music"


def test_frame_labels_stitch_across_chunks(tmp_path: Path) -> None:
    keep = 60_000 // yamnet.HOP_MS
    calls = [[row({MUSIC: 0.9})] * (keep + 4), [row({SPEECH: 0.9})] * 10]
    model = FakeYamnet(calls)
    opts = YamnetOptions.model_validate({"chunk_s": 60, "majority_frames": 1})
    labels = yamnet.frame_labels(tone(tmp_path / "a.flac", 65), 65_000, model, opts)
    assert len(labels) == keep + 10
    assert set(labels[:keep]) == {"music"}
    assert set(labels[keep:]) == {"speech"}
