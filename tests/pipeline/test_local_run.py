# SPDX-License-Identifier: AGPL-3.0-only
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from georg.cli import parse_boundaries
from georg.core.config import ModuleConfig, ProcessorsConfig, default_renditions
from georg.core.errors import ConfigError
from georg.core.types import (
    AudioSegment,
    BoundaryProposal,
    FormatContext,
    FormattedTranscript,
    MetadataContext,
    RemoteMedia,
    SermonMetadata,
    ServiceBoundaries,
    TimeRange,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from georg.media import ffmpeg
from georg.media.ffmpeg import FFMPEG, probe
from georg.pipeline import Processors, StageCache, build_processor, build_processors, run_local

PROPOSED = ServiceBoundaries(
    service_start_ms=1_000, sermon_start_ms=4_000, sermon_end_ms=8_000, service_end_ms=11_000
)


def tone(path: Path, seconds: int, freq: int) -> Path:
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration={seconds}", "-ac", "2", str(path)],
        check=True,
    )  # fmt: skip
    return path


class Counting:
    calls = 0


class FakeClassifier(Counting):
    version = "fake-classifier-1"

    def classify(self, audio: Path) -> list[AudioSegment]:
        self.calls += 1
        return [AudioSegment(range=TimeRange(start_ms=0, end_ms=12_000), label="speech")]


class FakeDetector:
    version = "fake-detector-1"

    def detect(self, segments, duration_ms, transcript):  # type: ignore[no-untyped-def]
        assert transcript is not None
        return BoundaryProposal(boundaries=PROPOSED, confidence=0.9, explanation="fake")


class FakeTranscriber(Counting):
    version = "fake-transcriber-1"

    def transcribe(self, audio: Path, language: str | None) -> Transcript:
        self.calls += 1
        if audio.name == ".sermon-transcribe.flac":
            seg = [
                TranscriptSegment(
                    text="Grace to you.", range=TimeRange(start_ms=2_500, end_ms=3_500)
                )
            ]
        else:
            seg = [
                TranscriptSegment(text=t, range=TimeRange(start_ms=s, end_ms=e))
                for t, s, e in [("Welcome.", 1_500, 3_000), ("Grace to you.", 4_500, 7_000)]
            ]
        return Transcript(language=language or "en", segments=tuple(seg), provider="fake")


class FakeFormatter(Counting):
    version = "fake-formatter-1"

    def format(self, transcript: Transcript, ctx: FormatContext) -> FormattedTranscript:
        self.calls += 1
        heading = ctx.metadata.title or ctx.metadata.occasion
        return FormattedTranscript(markdown=f"# {heading}\n\n{transcript.text}\n",
                                   words_preserved=True)  # fmt: skip


class FakeSermonTranscriber(Counting):
    version = "fake-sermon-transcriber-1"

    def transcribe(self, audio: Path, language: str | None) -> Transcript:
        self.calls += 1
        word = TranscriptWord(text="Grace", range=TimeRange(start_ms=2_500, end_ms=2_900))
        segment = TranscriptSegment(
            text="Grace to you.",
            range=TimeRange(start_ms=2_500, end_ms=3_500),
            words=(word,),
        )
        return Transcript(language=language or "en", segments=(segment,), provider="fake-sermon")


class FailingSermonTranscriber(FakeSermonTranscriber):
    version = "failing-sermon-transcriber-1"

    def transcribe(self, audio: Path, language: str | None) -> Transcript:
        self.calls += 1
        raise RuntimeError("sermon pass unavailable")


class FakeMetadata:
    version = "fake-metadata-1"

    calls = 0
    transcript: Transcript | None = None

    def generate(self, ctx: MetadataContext) -> SermonMetadata:
        self.calls += 1
        self.transcript = ctx.transcript
        docs = ctx.source.hints["documents"]
        return ctx.defaults.model_copy(update={"occasion": docs[0], "preacher": "Eric"})


def processors(
    formatter: bool = True, sermon_transcriber: FakeSermonTranscriber | None = None
) -> Processors:
    return Processors(
        classifier=FakeClassifier(),
        detector=FakeDetector(),
        transcriber=FakeTranscriber(),
        formatter=FakeFormatter() if formatter else None,
        metadata=FakeMetadata(),
        sermon_transcriber=sermon_transcriber,
    )


@pytest.fixture
def parts(tmp_path: Path) -> list[Path]:
    return [tone(tmp_path / "a.wav", 5, 440), tone(tmp_path / "b.wav", 7, 660)]


def test_run_local_joins_parts_and_writes_outputs(tmp_path: Path, parts: list[Path]) -> None:
    procs = processors()
    out = tmp_path / "out"
    result = run_local(parts, out, procs, cache=StageCache(tmp_path / "cache"),
                       renditions=default_renditions(), language="en",
                       service_date=date(2026, 11, 1), documents=["All Saints"])  # fmt: skip
    assert result.boundaries == PROPOSED
    assert result.metadata.occasion == "All Saints"
    assert procs.transcriber.calls == 2  # type: ignore[attr-defined]
    assert (out / "transcript.md").read_text() == "# All Saints\n\nGrace to you.\n"
    assert abs(probe(out / "sermon.mp3").duration_ms - 4_000) < 200
    assert abs(probe(out / "service_hq.mp3").duration_ms - 10_000) < 200
    assert (out / "service_phone.mp3").exists()
    assert not list(out.glob(".*"))
    report = json.loads((out / "report.json").read_text())
    assert report["proposal"]["confidence"] == 0.9
    assert report["transcript"]["segments"][0]["text"] == "Welcome."
    assert report["words_preserved"] is True


def test_run_local_caches_expensive_stages(tmp_path: Path, parts: list[Path]) -> None:
    procs = processors()
    cache = StageCache(tmp_path / "cache")
    kwargs = {"cache": cache, "renditions": default_renditions()[:1], "documents": ["x"]}
    run_local(parts, tmp_path / "one", procs, **kwargs)  # type: ignore[arg-type]
    override = PROPOSED.model_copy(update={"sermon_start_ms": 3_000})
    second = run_local(parts, tmp_path / "two", procs, boundaries=override, **kwargs)  # type: ignore[arg-type]
    assert second.boundaries == override
    assert second.timings["extract"] == second.timings["classify"] == 0.0
    assert second.timings["transcribe"] == 0.0
    assert procs.classifier.calls == 1  # type: ignore[attr-defined]
    assert procs.transcriber.calls == 3  # type: ignore[attr-defined]
    assert procs.formatter.calls == 2  # type: ignore[union-attr]
    assert (tmp_path / "two" / "transcript.md").read_text().endswith("Grace to you.\n")


def test_preview_is_the_whole_recording_cached_by_source(tmp_path: Path, parts: list[Path]) -> None:
    cache = StageCache(tmp_path / "cache")
    kwargs = {"cache": cache, "renditions": default_renditions()[:1], "documents": ["x"]}
    first = run_local(parts, tmp_path / "one", processors(), **kwargs)  # type: ignore[arg-type]
    preview = first.files["preview"]
    info = probe(preview)
    assert abs(info.duration_ms - first.duration_ms) < 100  # 0 to the end, not cut
    assert abs(info.duration_ms - 12_000) < 200
    assert (info.channels, info.sample_rate) == (1, 22050)
    mtime = preview.stat().st_mtime_ns
    override = PROPOSED.model_copy(update={"service_start_ms": 3_000})
    second = run_local(parts, tmp_path / "two", processors(), boundaries=override, **kwargs)  # type: ignore[arg-type]
    assert second.files["preview"] == preview
    assert second.timings["preview"] == 0.0
    assert preview.stat().st_mtime_ns == mtime


def test_remote_probe_cache_uses_hashed_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part = RemoteMedia(
        url="https://example.com/part.mp4",
        name="part.mp4",
        key="drive\0folder/file\0size\0checksum",
        headers=lambda *, force_refresh=False: {},
    )
    info = ffmpeg.MediaInfo(
        duration_ms=12_000, has_audio=True, has_video=False, sample_rate=44_100, channels=2
    )
    probes = 0
    extracts = 0

    def fake_probe_media(media: RemoteMedia, *, retryable: bool = False) -> ffmpeg.MediaInfo:
        nonlocal probes
        probes += 1
        assert media is part
        assert retryable
        return info

    def fake_extract_audio(parts: list[RemoteMedia], dest: Path, **kwargs: object) -> Path:
        nonlocal extracts
        extracts += 1
        assert parts == [part]
        assert kwargs["remote_probes"] == {part.key: info}
        return dest

    def stop_after_extract(path: Path) -> ffmpeg.MediaInfo:
        raise RuntimeError("stop after extract")

    monkeypatch.setattr(ffmpeg, "probe_media", fake_probe_media)
    monkeypatch.setattr(ffmpeg, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(ffmpeg, "probe", stop_after_extract)

    cache = StageCache(tmp_path / "cache")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="stop after extract"):
            run_local(
                [part],
                tmp_path / "out",
                processors(),
                cache=cache,
                renditions=default_renditions()[:1],
                documents=["x"],
            )

    assert probes == 1
    assert extracts == 2
    probe_files = list((cache.root / "probe").iterdir())
    assert [path.name for path in probe_files] == [f"{StageCache.key(part.key)}.json"]


def test_run_local_without_formatter_writes_plain_transcript(
    tmp_path: Path, parts: list[Path]
) -> None:
    run_local(parts, tmp_path / "out", processors(formatter=False),
              cache=StageCache(tmp_path / "cache"), renditions=default_renditions()[:1],
              documents=["x"])  # fmt: skip
    assert (tmp_path / "out" / "transcript.md").read_text() == "Grace to you.\n"


def test_sermon_transcriber_shifts_and_trims_times(tmp_path: Path, parts: list[Path]) -> None:
    sermon = FakeSermonTranscriber()
    procs = processors(sermon_transcriber=sermon)
    run_local(
        parts,
        tmp_path / "out",
        procs,
        cache=StageCache(tmp_path / "cache"),
        renditions=default_renditions()[:1],
        documents=["x"],
    )
    transcript = procs.metadata.transcript  # type: ignore[attr-defined]
    assert transcript is not None
    assert transcript.segments[0].range == TimeRange(start_ms=4_500, end_ms=4_900)
    assert transcript.segments[0].words[0].range == TimeRange(start_ms=4_500, end_ms=4_900)


def test_sermon_transcription_cache_uses_cut_range(tmp_path: Path, parts: list[Path]) -> None:
    sermon = FakeSermonTranscriber()
    procs = processors(sermon_transcriber=sermon)
    cache = StageCache(tmp_path / "cache")
    kwargs = {"cache": cache, "renditions": default_renditions()[:1], "documents": ["x"]}
    run_local(parts, tmp_path / "one", procs, **kwargs)  # type: ignore[arg-type]
    run_local(parts, tmp_path / "two", procs, **kwargs)  # type: ignore[arg-type]
    assert sermon.calls == 1
    changed = PROPOSED.model_copy(update={"sermon_start_ms": 3_000})
    run_local(parts, tmp_path / "three", procs, boundaries=changed, **kwargs)  # type: ignore[arg-type]
    assert sermon.calls == 2


def test_sermon_transcription_failure_falls_back(
    tmp_path: Path, parts: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    sermon = FailingSermonTranscriber()
    procs = processors(sermon_transcriber=sermon)
    run_local(
        parts,
        tmp_path / "out",
        procs,
        cache=StageCache(tmp_path / "cache"),
        renditions=default_renditions()[:1],
        documents=["x"],
    )
    transcript = procs.metadata.transcript  # type: ignore[attr-defined]
    assert transcript is not None
    assert transcript.text == "Grace to you."
    assert "sermon transcription failed" in caplog.text


def test_build_processors_from_config() -> None:
    config = ProcessorsConfig(
        classifier=ModuleConfig(type="ina_speech_segmenter"),
        detector=ModuleConfig(type="heuristic", options={"quiet_window_ms": 300_000}),
        transcriber=ModuleConfig(type="faster_whisper", options={"model": "small"}),
        sermon_transcriber=ModuleConfig(type="faster_whisper", options={"model": "large-v3"}),
        formatter=ModuleConfig(type="openai_chat", options={"api_key": "x"}),
        metadata=ModuleConfig(type="rules"),
    )
    built = build_processors(config, formatter=False)
    assert built.formatter is None
    assert "small" in built.transcriber.version
    assert built.sermon_transcriber is not None
    assert "large-v3" in built.sermon_transcriber.version
    assert built.detector.params.quiet_window_ms == 300_000  # type: ignore[attr-defined]


def test_unknown_type_is_config_error() -> None:
    with pytest.raises(ConfigError, match="known: faster_whisper, openai_compatible"):
        build_processor("transcriber", ModuleConfig(type="nope"))


def test_parse_boundaries() -> None:
    b = parse_boundaries("0:05,10:00,30:00,1:10:00")
    assert (b.service_start_ms, b.service_end_ms) == (5_000, 4_200_000)
