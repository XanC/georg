# SPDX-License-Identifier: AGPL-3.0-only
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from georg.core.contracts import Transcriber
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import TimeRange, TranscriptSegment
from georg.modules.processors.transcribe import FasterWhisperOptions, FasterWhisperTranscriber
from georg.modules.processors.transcribe.faster_whisper import (
    decode_pcm,
    drop_repeated_segments,
)


@dataclass
class Word:
    start: float
    end: float
    word: str
    probability: float


@dataclass
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float
    words: list[Any] | None = None


@dataclass
class Info:
    language: str


@dataclass
class FakeModel:
    segments: list[Segment]
    language: str = "en"
    calls: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)
    error: Exception | None = None

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Iterable[Segment], Info]:
        self.calls.append((audio, kwargs))
        if self.error is not None:
            raise self.error

        def lazy() -> Iterable[Segment]:
            yield from self.segments

        return lazy(), Info(self.language)


def make(fake: FakeModel, **options: object) -> tuple[FasterWhisperTranscriber, list[Any]]:
    loads: list[FasterWhisperOptions] = []

    def factory(opts: FasterWhisperOptions) -> FakeModel:
        loads.append(opts)
        return fake

    t = FasterWhisperTranscriber(
        dict(options), model_factory=factory, audio_decoder=lambda path: f"pcm:{path.name}"
    )
    return t, loads


def test_implements_protocol_and_maps_segments(tmp_path: Path) -> None:
    model = FakeModel(
        [
            Segment(3.0, 4.5, " Amen. ", -0.2),
            Segment(0.0, 2.25, " In the name of the Father ", -0.05),
        ]
    )
    t, _ = make(model)
    assert isinstance(t, Transcriber)
    out = t.transcribe(tmp_path / "s.flac", "en")
    assert [s.text for s in out.segments] == ["In the name of the Father", "Amen."]
    assert out.segments[0].range.start_ms == 0
    assert out.segments[0].range.end_ms == 2250
    assert out.segments[1].avg_logprob == -0.2
    assert out.language == "en"
    assert out.provider == "faster_whisper"
    assert out.model == "large-v3-turbo"


def test_passes_options_and_language_fallback(tmp_path: Path) -> None:
    model = FakeModel([], language="de")
    t, loads = make(model, language="en", prompt="St. Paul", beam_size=2, vad_filter=True)
    out = t.transcribe(tmp_path / "s.flac", None)
    samples, kwargs = model.calls[0]
    assert samples == "pcm:s.flac"
    assert kwargs == {
        "language": "en",
        "initial_prompt": "St. Paul",
        "beam_size": 2,
        "vad_filter": True,
        "word_timestamps": True,
        "condition_on_previous_text": False,
    }
    assert out.language == "en"
    assert out.segments == ()
    t.transcribe(tmp_path / "s.flac", None)
    assert len(loads) == 1


def test_detected_language_used_when_none_given(tmp_path: Path) -> None:
    t, _ = make(FakeModel([], language="es"))
    assert t.transcribe(tmp_path / "s.flac", None).language == "es"


def test_words_and_degenerate_ranges(tmp_path: Path) -> None:
    words = [Word(1.0, 1.0, " Grace", 1.2), Word(1.1, 1.4, " to", 0.8)]
    t, _ = make(FakeModel([Segment(1.0, 1.0, "Grace to", -0.1, words)]), word_timestamps=True)
    seg = t.transcribe(tmp_path / "s.flac", "en").segments[0]
    assert seg.range.end_ms == 1001
    assert [w.text for w in seg.words] == [" Grace", " to"]
    assert seg.words[0].probability == 1.0
    assert seg.words[0].range.end_ms == 1001


def test_transcribe_defaults_and_each_new_option(tmp_path: Path) -> None:
    model = FakeModel([])
    t, _ = make(model)
    t.transcribe(tmp_path / "s.flac", "en")
    assert model.calls[0][1] == {
        "language": "en",
        "initial_prompt": None,
        "beam_size": 5,
        "vad_filter": True,
        "word_timestamps": True,
        "condition_on_previous_text": False,
    }

    model = FakeModel([])
    t, _ = make(
        model,
        vad_filter=False,
        vad_parameters={"threshold": 0.4},
        condition_on_previous_text=True,
        hallucination_silence_threshold=1.5,
        hotwords="Eucharist",
        word_timestamps=True,
    )
    t.transcribe(tmp_path / "s.flac", "en")
    assert model.calls[0][1] == {
        "language": "en",
        "initial_prompt": None,
        "beam_size": 5,
        "vad_filter": False,
        "vad_parameters": {"threshold": 0.4},
        "word_timestamps": True,
        "condition_on_previous_text": True,
        "hallucination_silence_threshold": 1.5,
        "hotwords": "Eucharist",
    }


def test_version_reflects_parameters() -> None:
    a, _ = make(FakeModel([]))
    b, _ = make(FakeModel([]), model="small", compute_type="float16")
    assert a.version != b.version
    assert "small-float16" in b.version


def test_version_changes_with_each_output_option() -> None:
    base = make(FakeModel([]))[0].version
    for options in [
        {"vad_filter": False},
        {"vad_parameters": {"threshold": 0.4}},
        {"condition_on_previous_text": True},
        {"hallucination_silence_threshold": 1.5},
        {"hotwords": "Eucharist"},
        {"prompt": "St. Paul"},
        {"word_timestamps": False},
        {"split_gap_seconds": 1.5},
    ]:
        assert make(FakeModel([]), **options)[0].version != base, options


def test_stretched_segment_splits_at_word_gap(tmp_path: Path) -> None:
    # A real 87-min service produced this: VAD let one segment stretch across a
    # long non-speech region, hiding the sermon's closing words.
    stretched = Segment(
        2444.9,
        2665.08,
        " our Lord. Amen. For the right of the reception of new members, "
        "I'd like to invite Ryan Buck",
        -0.1,
        [
            Word(2444.9, 2445.2, " our", 0.9),
            Word(2445.2, 2445.6, " Lord.", 0.9),
            Word(2445.6, 2445.9, " Amen.", 0.9),
            Word(2664.1, 2664.5, " For", 0.8),
            Word(2664.5, 2664.8, " the", 0.8),
            Word(2664.8, 2665.08, " right", 0.8),
        ],
    )
    t, _ = make(FakeModel([stretched]))
    out = t.transcribe(tmp_path / "s.flac", "en")
    assert [s.text for s in out.segments] == ["our Lord. Amen.", "For the right"]
    first, second = out.segments
    assert first.range == TimeRange(start_ms=2444900, end_ms=2445900)
    assert second.range == TimeRange(start_ms=2664100, end_ms=2665080)
    assert [w.text for w in first.words] == [" our", " Lord.", " Amen."]


def test_gap_split_threshold_and_disable(tmp_path: Path) -> None:
    words = [
        Word(0.0, 0.5, " Hello", 0.9),
        Word(2.0, 2.5, " there", 0.9),  # 1.5 s gap
    ]
    t, _ = make(FakeModel([Segment(0.0, 2.5, " Hello there", -0.1, words)]))
    out = t.transcribe(tmp_path / "s.flac", "en")
    assert len(out.segments) == 1  # 1.5 s < default 2.0 s gap

    t, _ = make(
        FakeModel([Segment(0.0, 2.5, " Hello there", -0.1, words)]),
        split_gap_seconds=1.0,
    )
    out = t.transcribe(tmp_path / "s.flac", "en")
    assert [s.text for s in out.segments] == ["Hello there"]

    t, _ = make(
        FakeModel([Segment(0.0, 2.5, " Hello there", -0.1, words)]),
        split_gap_seconds=0.0,
    )
    out = t.transcribe(tmp_path / "s.flac", "en")
    assert len(out.segments) == 1  # 0 disables splitting


def test_short_leading_fragment_merges_into_following_piece(tmp_path: Path) -> None:
    words = [
        Word(1478.260, 1478.860, " Dear", 0.9),
        Word(1760.940, 1761.540, " Saints,", 0.9),
        Word(1761.680, 1761.780, " there", 0.9),
        Word(1761.900, 1762.000, " are", 0.9),
    ]
    t, _ = make(FakeModel([Segment(1478.260, 1762.000, "", -0.1, words)]))

    out = t.transcribe(tmp_path / "s.flac", "en")

    assert [s.text for s in out.segments] == ["Dear Saints, there are"]
    assert out.segments[0].range.start_ms == 1760340
    assert out.segments[0].range.end_ms == 1762000
    assert out.segments[0].words[0].range == TimeRange(start_ms=1760340, end_ms=1760940)


def test_short_trailing_fragment_merges_into_previous_piece(tmp_path: Path) -> None:
    words = [
        Word(100.000, 100.500, " In", 0.9),
        Word(100.600, 101.000, " the", 0.9),
        Word(101.100, 101.500, " name", 0.9),
        Word(104.000, 104.500, " Dear", 0.9),
    ]
    t, _ = make(FakeModel([Segment(100.000, 104.500, "", -0.1, words)]), split_gap_seconds=1)

    out = t.transcribe(tmp_path / "s.flac", "en")

    assert [s.text for s in out.segments] == ["In the name Dear"]
    assert out.segments[0].range == TimeRange(start_ms=100000, end_ms=102000)
    assert out.segments[0].words[-1].range == TimeRange(start_ms=101500, end_ms=102000)


def test_short_fragment_after_sentence_stays_separate(tmp_path: Path) -> None:
    words = [
        Word(100.000, 100.500, " In", 0.9),
        Word(100.600, 101.000, " the", 0.9),
        Word(101.100, 101.500, " Amen.", 0.9),
        Word(104.000, 104.500, " Dear", 0.9),
    ]
    t, _ = make(FakeModel([Segment(100.000, 104.500, "", -0.1, words)]), split_gap_seconds=1)

    out = t.transcribe(tmp_path / "s.flac", "en")

    assert [s.text for s in out.segments] == ["In the Amen.", "Dear"]


def test_invalid_options() -> None:
    with pytest.raises(ConfigError):
        FasterWhisperTranscriber({"beam_size": 0})
    with pytest.raises(ConfigError):
        FasterWhisperTranscriber({"unknown": 1})


def seg(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        text=text,
        range=TimeRange(start_ms=int(start * 1000), end_ms=int(end * 1000)),
        avg_logprob=-0.1,
        words=(),
    )


def test_guard_drops_thank_you_loop() -> None:
    loop = [seg(i * 30.0, (i + 1) * 30.0, " Thank you. ") for i in range(4)]
    speech = seg(120.0, 128.0, "In the name of the Father")
    out = drop_repeated_segments([speech, *loop])
    assert [s.text for s in out] == ["In the name of the Father"]


def test_guard_drops_lone_long_hallucination() -> None:
    out = drop_repeated_segments([seg(0.0, 30.0, " CHOIR SINGS "), seg(30.0, 35.0, "Let us pray.")])
    assert [s.text for s in out] == ["Let us pray."]


def test_guard_keeps_short_repeats_and_speech() -> None:
    amens = [seg(float(i), float(i) + 1.0, "Amen.") for i in range(3)]
    repeats = [seg(float(i), float(i) + 1.0, "Lord, hear our prayer.") for i in range(3)]
    out = drop_repeated_segments(amens + repeats)
    assert len(out) == 6


def test_guard_keeps_long_repeated_speech() -> None:
    # Same words but a real phrase with many words is not in the hallucination
    # set; a run spanning <20 s also survives.
    segs = [seg(float(i * 5), float(i * 5) + 4.0, "Glory to God in the highest") for i in range(3)]
    assert len(drop_repeated_segments(segs)) == 3


def test_model_errors_are_module_errors(tmp_path: Path) -> None:
    t, _ = make(FakeModel([], error=RuntimeError("boom")))
    with pytest.raises(ModuleError, match="boom"):
        t.transcribe(tmp_path / "s.flac", "en")


def test_missing_library_reports_extra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "faster_whisper":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    t = FasterWhisperTranscriber(audio_decoder=lambda _path: None)
    with pytest.raises(ModuleError, match="whisper"):
        t.transcribe(tmp_path / "s.flac", "en")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_decode_pcm(tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    src = tmp_path / "tone.mp3"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:sample_rate=44100:duration=1.5", "-ac", "2", str(src)],
        check=True,
    )  # fmt: skip
    pcm = decode_pcm(src)
    assert pcm.dtype.name == "float32"
    assert abs(len(pcm) - 24_000) < 2_000
    with pytest.raises(ModuleError):
        decode_pcm(tmp_path / "missing.mp3")
