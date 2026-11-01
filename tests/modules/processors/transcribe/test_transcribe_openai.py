# SPDX-License-Identifier: AGPL-3.0-only
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from georg.core.contracts import Transcriber
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import TimeRange
from georg.modules.processors.transcribe import OpenAICompatibleTranscriber, parse_verbose_json

FIXTURES = Path(__file__).parent / "fixtures"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def form_fields(request: httpx.Request) -> dict[str, list[str]]:
    """Decode the non-file fields of a multipart body."""
    body = request.read()
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    fields: dict[str, list[str]] = {}
    for part in body.split(b"--" + boundary):
        head, _, value = part.partition(b"\r\n\r\n")
        if b'name="' not in head or b"filename=" in head:
            continue
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        fields.setdefault(name, []).append(value.rstrip(b"\r\n").decode())
    return fields


def make(
    handler: Callable[[httpx.Request], httpx.Response], **options: Any
) -> OpenAICompatibleTranscriber:
    return OpenAICompatibleTranscriber(
        {"api_key": "env:GEORG_TEST_TRANSCRIBE_KEY", **options},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEORG_TEST_TRANSCRIBE_KEY", "test-key")


def wav(path: Path, seconds: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:sample_rate=16000:duration={seconds}", str(path)],
        check=True,
    )  # fmt: skip
    return path


def test_parse_segment_words_and_degenerate_times() -> None:
    segments = parse_verbose_json(fixture("lemonfox_verbose.json"))
    assert [s.text for s in segments] == [
        "Grace to you and peace from God our Father.",
        "Today is All Saints Day.",
        "Let us pray.",
    ]
    assert segments[0].range == TimeRange(start_ms=0, end_ms=4200)
    assert segments[0].avg_logprob == pytest.approx(-0.12)
    assert [w.text for w in segments[0].words] == ["Grace", "to"]
    assert segments[0].words[1].probability == pytest.approx(0.98)
    assert segments[2].range == TimeRange(start_ms=20_000, end_ms=20_001)


def test_parse_assigns_top_level_words_to_segments() -> None:
    segments = parse_verbose_json(fixture("openai_top_level_words.json"))
    assert [[w.text for w in s.words] for s in segments] == [["Let", "us", "pray"], ["Amen"]]
    assert segments[1].words[0].range == TimeRange(start_ms=4200, end_ms=5000)


def test_parse_rejects_malformed() -> None:
    with pytest.raises(ModuleError):
        parse_verbose_json({"segments": [{"start": "zero", "end": 1, "text": "x"}]})


def test_direct_upload_request_and_mapping(tmp_path: Path) -> None:
    audio = tmp_path / "sermon.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=fixture("lemonfox_verbose.json"))

    transcriber = make(handler, prompt="A Lutheran sermon.")
    assert isinstance(transcriber, Transcriber)
    transcript = transcriber.transcribe(audio, "en")
    req = seen[0]
    assert str(req.url) == "https://api.lemonfox.ai/v1/audio/transcriptions"
    assert req.headers["authorization"] == "Bearer test-key"
    fields = form_fields(req)
    assert fields == {
        "response_format": ["verbose_json"],
        "language": ["en"],
        "prompt": ["A Lutheran sermon."],
    }
    assert b'filename="sermon.mp3"' in req.read() and b"ID3fake-mp3-bytes" in req.read()
    assert transcript.language == "en"
    assert transcript.provider == "api.lemonfox.ai"
    assert transcript.text.startswith("Grace to you")


def test_model_word_timestamps_and_reported_language(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=fixture("openai_top_level_words.json"))

    transcriber = make(
        handler, base_url="https://api.openai.com/v1", model="whisper-1", word_timestamps=True
    )
    transcript = transcriber.transcribe(audio, None)
    fields = form_fields(seen[0])
    assert fields["model"] == ["whisper-1"]
    assert fields["timestamp_granularities[]"] == ["segment", "word"]
    assert "language" not in fields
    assert transcript.language == "english"
    assert transcript.model == "whisper-1"


def test_retryable_and_permanent_failures(tmp_path: Path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=fixture("openai_top_level_words.json"))

    assert make(flaky).transcribe(audio, "en").segments
    assert len(calls) == 2

    with pytest.raises(ModuleError) as info:
        make(lambda _: httpx.Response(429), attempts=2).transcribe(audio, "en")
    assert info.value.retryable is True

    with pytest.raises(ModuleError) as info:
        make(lambda _: httpx.Response(400, json={"error": {"message": "bad"}})).transcribe(
            audio, "en"
        )
    assert info.value.retryable is False


def test_missing_secret_and_bad_options() -> None:
    with pytest.raises(ConfigError, match="NOPE_UNSET"):
        OpenAICompatibleTranscriber({"api_key": "env:NOPE_UNSET"})
    with pytest.raises(ConfigError, match="options"):
        OpenAICompatibleTranscriber({"api_key": "k", "bogus": 1})
    with pytest.raises(ConfigError, match="overlap"):
        OpenAICompatibleTranscriber({"api_key": "k", "max_chunk_ms": 1000, "overlap_ms": 500})


@needs_ffmpeg
def test_long_audio_is_chunked_and_offsets_restored(tmp_path: Path) -> None:
    audio = wav(tmp_path / "service.wav", 25)
    uploads: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        uploads.append(request.read())
        n = len(uploads) - 1
        # Each chunk reports a segment in its overlap region with the previous chunk and
        # one in its own middle; times are chunk-relative.
        segments = [
            {"start": 0.2, "end": 1.0, "text": f"overlap {n}", "avg_logprob": -0.1},
            {"start": 3.0, "end": 5.0, "text": f"middle {n}", "avg_logprob": -0.2},
        ]
        return httpx.Response(200, json={"language": "english", "segments": segments})

    transcriber = make(handler, max_chunk_ms=10_000, overlap_ms=2_000)
    transcript = transcriber.transcribe(audio, "en")
    # Chunks: [0, 10s), [8s, 18s), [16s, 25s). Cuts at 9 s and 17 s.
    assert len(uploads) == 3
    assert all(b'filename="chunk' in body and b"audio/mpeg" in body for body in uploads)
    assert [(s.text, s.range.start_ms, s.range.end_ms) for s in transcript.segments] == [
        ("overlap 0", 200, 1_000),
        ("middle 0", 3_000, 5_000),
        ("middle 1", 11_000, 13_000),
        ("middle 2", 19_000, 21_000),
    ]
    assert not list(tmp_path.glob("*chunk*"))


@needs_ffmpeg
def test_oversized_file_is_reencoded_to_mp3(tmp_path: Path) -> None:
    audio = wav(tmp_path / "service.wav", 3)
    names: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        names.append(request.read().split(b'filename="')[1].split(b'"')[0])
        return httpx.Response(200, json={"segments": [{"start": 0, "end": 2, "text": "hi"}]})

    transcript = make(handler, max_upload_bytes=60_000, overlap_ms=0).transcribe(audio, "en")
    assert names == [b"chunk000.mp3"]
    assert transcript.text == "hi"
