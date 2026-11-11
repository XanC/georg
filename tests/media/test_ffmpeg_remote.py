# SPDX-License-Identifier: AGPL-3.0-only
"""`extract_audio`/`probe_media` on `RemoteMedia` inputs, against a local HTTP server.

The server requires a bearer token and honors `Range`, like Drive's `alt=media`
endpoint; no real Google calls or credentials are involved.
"""

import json
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from georg.core.types import RemoteMedia
from georg.media import MediaError, MediaInfo, extract_audio, ffmpeg, probe, probe_media
from georg.media.range_proxy import serve_remote

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

SECONDS = 8


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def _quota(self) -> None:
        status, body = QUOTA_403
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.server.token}"

    def _send(self, start: int, end: int, *, head: bool = False, data: bytes | None = None) -> None:
        data = self.server.content if data is None else data
        if self.server.truncate is not None:
            data = data[: self.server.truncate]
        total = len(data)
        end = min(end, total - 1)
        partial = "range" in self.headers
        self.send_response(206 if partial else 200)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if not head:
            body = data[start : end + 1]
            self.server.bytes_sent += len(body)
            if self.server.drop_after is not None and start == 0:
                drop = min(self.server.drop_after, len(body))
                self.server.drop_after = None
                self.wfile.write(body[:drop])
                self.wfile.flush()
                self.connection.shutdown(2)
                self.connection.close()
                return
            self.wfile.write(body)

    def _handle(self, head: bool) -> None:
        self.server.requests.append(self)
        with self.server.lock:
            self.server.in_flight += 1
            self.server.max_in_flight = max(self.server.max_in_flight, self.server.in_flight)
        try:
            self._respond(head)
        finally:
            with self.server.lock:
                self.server.in_flight -= 1

    def _respond(self, head: bool) -> None:
        if self.server.latency:
            time.sleep(self.server.latency)
        if not self._authorized():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if (error := self.server.errors.get(self.path.lstrip("/"))) is not None:
            status, body = error
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if not head:
                self.wfile.write(payload)
            return
        data = self.server.contents.get(self.path.lstrip("/"), self.server.content)
        if self.server.require_closed is not None:
            # Like Drive under quota: only closed, small-enough ranges are served.
            m = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            if not m or int(m[2]) - int(m[1]) + 1 > self.server.require_closed:
                self._quota()
                return
        start, end = 0, len(data) - 1
        if m := re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "")):
            start = int(m[1])
            if m[2]:
                end = int(m[2])
        if self.server.quota_from is not None and (
            self.server.quota_burned or start >= self.server.quota_from
        ):
            # Drive counts every served range against the file quota; once it
            # is exceeded, even a tiny error-probe range gets the same 403.
            self.server.quota_burned = True
            self._quota()
            return
        self._send(start, end, head=head, data=data)

    def do_GET(self) -> None:
        self._handle(head=False)

    def do_HEAD(self) -> None:
        self._handle(head=True)

    def log_message(self, *args: object) -> None:
        pass


class _Server(ThreadingHTTPServer):
    token = "good-token"
    content: bytes
    contents: dict[str, bytes]
    """Alternative payloads keyed by request path (e.g. a moov-at-end file)."""
    truncate: int | None = None
    """Serve only this many leading bytes, as if Drive closed the stream early."""
    require_closed: int | None = None
    """When set, only closed `Range`s no longer than this get a 206; else 403."""
    bytes_sent: int
    """Payload bytes actually written to clients (what upstream 'downloaded')."""
    requests: list[BaseHTTPRequestHandler]
    errors: dict[str, tuple[int, dict[str, object]]]
    latency: float
    """Seconds added to every upstream response, like a high-latency Drive."""
    quota_from: int | None
    """Requests at or past this offset get a quota 403 (Drive's counted bytes)."""
    quota_burned: bool
    """Once a quota 403 has been served, every later request gets one too."""
    lock: threading.Lock
    in_flight: int
    max_in_flight: int
    """Peak simultaneous upstream requests."""
    drop_after: int | None


QUOTA_403 = (
    403,
    {
        "error": {
            "code": 403,
            "message": "The download quota for this file has been exceeded.",
            "errors": [{"domain": "usageLimits", "reason": "downloadQuotaExceeded"}],
        }
    },
)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_Server]:
    video = tmp_path / "part.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={SECONDS}",
         "-f", "lavfi", "-i", f"color=size=64x64:rate=10:duration={SECONDS}",
         "-shortest", "-c:v", "libx264", "-c:a", "aac",
         "-movflags", "+faststart", str(video)],
        check=True,
    )  # fmt: skip
    tail = tmp_path / "tail.mp4"  # same recording, moov atom at the end
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-c", "copy", str(tail)],
        check=True,
    )
    # High-bitrate video + sparse audio, moov atom at the end.
    interleaved = tmp_path / "interleaved.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=640x480:rate=30:duration={SECONDS}",
         "-f", "lavfi", "-i", f"sine=frequency=660:sample_rate=48000:duration={SECONDS}",
         "-t", str(SECONDS), "-c:v", "libx264",
         "-x264-params", "keyint=30:min-keyint=30:scenecut=0",
         "-b:v", "8M", "-minrate", "8M", "-maxrate", "8M", "-bufsize", "8M",
         "-c:a", "aac", "-b:a", "128k", str(interleaved)],
        check=True,
    )  # fmt: skip
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.content = video.read_bytes()
    httpd.contents = {"tail.mp4": tail.read_bytes(), "interleaved.mp4": interleaved.read_bytes()}
    httpd.bytes_sent = 0
    httpd.requests = []
    httpd.errors = {}
    httpd.require_closed = None
    httpd.latency = 0.0
    httpd.quota_from = None
    httpd.quota_burned = False
    httpd.lock = threading.Lock()
    httpd.in_flight = 0
    httpd.max_in_flight = 0
    httpd.drop_after = None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def remote(server: _Server, name: str = "part.mp4") -> RemoteMedia:
    def headers(*, force_refresh: bool = False) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {server.token}"}

    return RemoteMedia(
        url=f"http://127.0.0.1:{server.server_port}/{name}",
        name=name,
        key="key-" + name,
        headers=headers,
        size=len(server.contents.get(name, server.content)),
    )


def test_extract_streams_remote_part(server: _Server, tmp_path: Path) -> None:
    part = remote(server)
    out = extract_audio([part], tmp_path / "service.flac", sleep=lambda _: None)
    info = probe(out)
    assert not info.has_video
    assert abs(info.duration_ms - SECONDS * 1000) < 500
    assert server.requests
    assert all(r.headers.get("Authorization") == f"Bearer {server.token}" for r in server.requests)


def test_probe_remote_reads_streams(server: _Server) -> None:
    info = probe_media(remote(server))
    assert info.has_audio and info.has_video


def test_token_refresh_recovers_from_expired_token(server: _Server, tmp_path: Path) -> None:
    refreshed: list[bool] = []

    def headers(*, force_refresh: bool = False) -> Mapping[str, str]:
        refreshed.append(force_refresh)
        return {"Authorization": f"Bearer {server.token if force_refresh else 'stale-token'}"}

    part = RemoteMedia(
        url=f"http://127.0.0.1:{server.server_port}/p.mp4",
        name="p.mp4",
        key="k",
        headers=headers,
    )
    out = extract_audio([part], tmp_path / "s.flac", sleep=lambda _: None)
    assert probe(out).has_audio
    assert any(refreshed), "a retry must force a token refresh"


def test_unauthorized_surfaces_without_token(server: _Server, tmp_path: Path) -> None:
    def headers(*, force_refresh: bool = False) -> Mapping[str, str]:
        return {"Authorization": "Bearer always-wrong-token"}

    part = RemoteMedia(
        url=f"http://127.0.0.1:{server.server_port}/p.mp4",
        name="p.mp4",
        key="k",
        headers=headers,
    )
    with pytest.raises(MediaError) as exc:
        extract_audio([part], tmp_path / "s.flac", sleep=lambda _: None)
    assert exc.value.retryable
    assert "always-wrong-token" not in str(exc.value)
    assert not list(tmp_path.glob("*.flac")) and not list(tmp_path.glob(".*partial*"))


def test_failed_remote_extract_leaves_no_partial(server: _Server, tmp_path: Path) -> None:
    server.token = "required-token"

    def headers(*, force_refresh: bool = False) -> Mapping[str, str]:
        return {"Authorization": "Bearer wrong"}

    part = RemoteMedia(
        url=f"http://127.0.0.1:{server.server_port}/p.mp4",
        name="p.mp4",
        key="k",
        headers=headers,
    )
    with pytest.raises(MediaError):
        extract_audio([part], tmp_path / "s.flac", attempts=1, sleep=lambda _: None)
    assert not list(tmp_path.glob("*.flac")) and not list(tmp_path.glob(".*partial*"))


def test_extract_joins_remote_parts_in_order(server: _Server, tmp_path: Path) -> None:
    first = remote(server, "01.mp4")
    second = remote(server, "02.mp4")
    out = extract_audio([first, second], tmp_path / "s.flac", sleep=lambda _: None)
    assert abs(probe(out).duration_ms - 2 * SECONDS * 1000) < 600


def test_truncated_stream_fails_retryable(server: _Server, tmp_path: Path) -> None:
    """A stream the server ends early produces short audio: retried, then raised."""
    server.truncate = len(server.content) // 3
    with pytest.raises(MediaError, match=r"extracted \d+\.\d s of expected") as exc:
        extract_audio([remote(server)], tmp_path / "s.flac", attempts=2, sleep=lambda _: None)
    assert exc.value.retryable
    assert not list(tmp_path.glob("*.flac")) and not list(tmp_path.glob(".*partial*"))


def _big_mp4(tmp_path: Path, seconds: int = 45) -> bytes:
    """A high-bitrate moov-at-end mp4 larger than TAIL_BYTES (16 MiB)."""
    big = tmp_path / "big.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=640x480:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=660:sample_rate=48000:duration={seconds}",
         "-t", str(seconds), "-c:v", "libx264", "-b:v", "8M",
         "-c:a", "aac", "-b:a", "128k", str(big)],
        check=True,
    )  # fmt: skip
    data = big.read_bytes()
    assert len(data) > 16 << 20
    return data


def test_single_body_request_and_tail_request(server: _Server, tmp_path: Path) -> None:
    """A full extraction reads the body through one stream + one tail range."""
    server.contents["big.mp4"] = _big_mp4(tmp_path)
    out = extract_audio([remote(server, "big.mp4")], tmp_path / "s.flac", sleep=lambda _: None)
    assert abs(probe(out).duration_ms - 45 * 1000) < 500
    ranges = [r.headers.get("Range", "") for r in server.requests]
    assert all(re.fullmatch(r"bytes=\d+-\d+", value) for value in ranges)
    # Each ffmpeg call = one tail fetch + one body stream; ffmpeg's re-read
    # of byte 48 replays from the recent-bytes buffer, no extra request.
    assert len(ranges) == 4, ranges
    assert server.bytes_sent <= 3 * len(server.contents["big.mp4"])


def test_quota_latch_stops_upstream_traffic(server: _Server, tmp_path: Path) -> None:
    """The first quota 403 latches and remains retryable."""
    server.errors["tail.mp4"] = QUOTA_403
    sleeps: list[float] = []
    with pytest.raises(MediaError) as exc:
        extract_audio([remote(server, "tail.mp4")], tmp_path / "s.flac",
                      attempts=3, sleep=sleeps.append)  # fmt: skip
    assert exc.value.retryable
    assert exc.value.retry_after == ffmpeg.DEFAULT_QUOTA_RETRY_AFTER
    assert not sleeps


def test_forward_seek_discards_from_one_stream(server: _Server) -> None:
    """A forward seek does not open another body request."""
    data = (bytes(range(256)) * ((20 << 20) // 256))[: 20 << 20]
    server.contents["big.bin"] = data
    with serve_remote(remote(server, "big.bin")) as url:
        first = httpx.get(url, headers={"Range": "bytes=0-1023"})
        later = httpx.get(url, headers={"Range": "bytes=4096-5119"})
    assert first.content == data[:1024]
    assert later.content == data[4096:5120]
    assert len(server.requests) == 2  # tail + one body stream


def test_retry_recovers_when_stream_restored(server: _Server, tmp_path: Path) -> None:
    """A short first attempt retries with refreshed credentials and succeeds."""
    server.truncate = len(server.content) // 3

    def headers(*, force_refresh: bool = False) -> Mapping[str, str]:
        if force_refresh:
            server.truncate = None  # Drive serves the whole file again
        return {"Authorization": f"Bearer {server.token}"}

    part = RemoteMedia(
        url=f"http://127.0.0.1:{server.server_port}/p.mp4",
        name="p.mp4",
        key="k",
        headers=headers,
    )
    out = extract_audio([part], tmp_path / "s.flac", sleep=lambda _: None)
    assert abs(probe(out).duration_ms - SECONDS * 1000) < 500


def test_http_error_surfaces_server_reason_and_retry_after(server: _Server, tmp_path: Path) -> None:
    """ffmpeg hides the response body; a follow-up Range request reads it."""
    server.errors["quota.mp4"] = QUOTA_403
    part = remote(server, "quota.mp4")
    part = RemoteMedia(
        url=part.url, name=part.name, key=part.key, headers=part.headers,
        quota_retry_after=timedelta(hours=6),
    )  # fmt: skip
    with pytest.raises(MediaError) as exc:
        extract_audio([part], tmp_path / "s.flac", sleep=lambda _: None)
    message = str(exc.value)
    assert "Server returned 403" in message
    assert "The download quota for this file has been exceeded." in message
    assert "downloadQuotaExceeded" in message
    assert server.token not in message
    assert exc.value.retryable
    assert exc.value.retry_after == timedelta(hours=6)


def test_quota_error_defaults_to_six_hour_retry_after(server: _Server, tmp_path: Path) -> None:
    server.errors["quota.mp4"] = QUOTA_403
    with pytest.raises(MediaError) as exc:
        probe_media(remote(server, "quota.mp4"))
    assert "downloadQuotaExceeded" in str(exc.value)
    assert exc.value.retry_after == ffmpeg.DEFAULT_QUOTA_RETRY_AFTER


def test_error_without_reason_reports_status_only(server: _Server, tmp_path: Path) -> None:
    """A 4xx whose body is not Drive-style JSON just reports the status."""
    server.errors["gone.mp4"] = (404, {"not": "drive-json"})
    with pytest.raises(MediaError) as exc:
        probe_media(remote(server, "gone.mp4"))
    assert "Server returned 404" in str(exc.value)
    assert exc.value.retry_after is None


def test_probe_media_remote_error_surfaces_reason(server: _Server) -> None:
    server.errors["quota.mp4"] = QUOTA_403
    with pytest.raises(MediaError) as exc:
        probe_media(remote(server, "quota.mp4"))
    assert "The download quota for this file has been exceeded." in str(exc.value)


def test_remote_probes_skip_probing(server: _Server, tmp_path: Path) -> None:
    """Precomputed probe results (the stage cache) mean no ffprobe call at all."""
    part = remote(server)
    probes = {
        part.key: MediaInfo(
            duration_ms=2000, has_audio=True, has_video=True, sample_rate=44100, channels=2
        )
    }

    def fail_probe(*args: object, **kwargs: object) -> MediaInfo:
        raise AssertionError("probe_media should not be called")

    original = ffmpeg.probe_media
    ffmpeg.probe_media = fail_probe  # type: ignore[assignment]
    try:
        out = extract_audio([part], tmp_path / "s.flac", sleep=lambda _: None,
                            remote_probes=probes)  # fmt: skip
    finally:
        ffmpeg.probe_media = original  # type: ignore[assignment]
    assert probe(out).has_audio


def test_retry_refreshes_token_for_error_lookup(server: _Server, tmp_path: Path) -> None:
    """A 401 still refreshes credentials; the failure then reports the real reason."""
    server.errors["p.mp4"] = QUOTA_403
    calls: list[bool] = []

    def headers(*, force_refresh: bool = False) -> Mapping[str, str]:
        calls.append(force_refresh)
        return {"Authorization": f"Bearer {server.token}"}

    part = RemoteMedia(
        url=f"http://127.0.0.1:{server.server_port}/p.mp4",
        name="p.mp4",
        key="k",
        headers=headers,
    )
    with pytest.raises(MediaError) as exc:
        extract_audio([part], tmp_path / "s.flac", attempts=1, sleep=lambda _: None)
    assert "The download quota for this file has been exceeded." in str(exc.value)


def test_resume_starts_at_exact_next_byte(server: _Server) -> None:
    """A dropped body stream resumes with the next byte and no corruption."""
    data = (bytes(range(256)) * ((20 << 20) // 256))[: 20 << 20]
    server.contents["big.bin"] = data
    server.drop_after = (3 << 20) + 65536  # mid-chunk, past a few full chunks
    with serve_remote(remote(server, "big.bin")) as url:
        response = httpx.get(url, headers={"Range": f"bytes=0-{len(data) - 1}"})
    assert response.status_code == 206
    assert response.content == data
    ranges = [r.headers.get("Range", "") for r in server.requests]
    assert len(ranges) == 3  # tail, dropped body, and one exact resume
    assert ranges[-1].startswith(f"bytes={3 << 20}-")  # next recorded byte


def test_seek_request_preempts_a_stalled_client(server: _Server, tmp_path: Path) -> None:
    """A new request never waits on another connection's stalled write.

    With a tiny proxy send buffer, ffmpeg's first connection backs up fast;
    the moov-at-end seek it opens next must preempt it — the deadlock this
    regresses hung the proxy forever in production.
    """
    server.contents["big.mp4"] = _big_mp4(tmp_path)
    with serve_remote(remote(server, "big.mp4"), send_buffer=8 << 10) as url:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                url,
                "-map",
                "0:a:0",
                "-c:a",
                "flac",
                str(tmp_path / "out.flac"),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert abs(probe(tmp_path / "out.flac").duration_ms - 45 * 1000) < 500
    ranges = [r.headers.get("Range", "") for r in server.requests]
    assert all(re.fullmatch(r"bytes=\d+-\d+", value) for value in ranges)
    assert len(ranges) <= 4  # tail + body (+ a resumed/reopened stream at most)


def test_stalled_upstream_fails_the_attempt(server: _Server) -> None:
    """No progress for `stall_seconds` ends the request instead of hanging."""
    data = (bytes(range(256)) * ((20 << 20) // 256))[: 20 << 20]
    server.contents["big.bin"] = data
    server.latency = 2.0
    started = time.monotonic()
    with serve_remote(remote(server, "big.bin"), stall_seconds=1.0) as url:
        response = httpx.get(url, headers={"Range": "bytes=0-1023"}, timeout=60)
    assert response.status_code >= 400
    assert time.monotonic() - started < 60
