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
from georg.modules.processors.transcribe import FasterWhisperOptions, FasterWhisperTranscriber
from georg.modules.processors.transcribe.faster_whisper import decode_pcm


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
        "word_timestamps": False,
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


def test_version_reflects_parameters() -> None:
    a, _ = make(FakeModel([]))
    b, _ = make(FakeModel([]), model="small", compute_type="float16")
    assert a.version != b.version
    assert "small-float16" in b.version


def test_invalid_options() -> None:
    with pytest.raises(ConfigError):
        FasterWhisperTranscriber({"beam_size": 0})
    with pytest.raises(ConfigError):
        FasterWhisperTranscriber({"unknown": 1})


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
