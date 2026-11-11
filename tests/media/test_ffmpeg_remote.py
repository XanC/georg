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
from collections.abc import Iterator, Mapping
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from georg.core.types import RemoteMedia
from georg.media import MediaError, MediaInfo, extract_audio, ffmpeg, probe, probe_media

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
            self.wfile.write(data[start : end + 1])

    def _handle(self, head: bool) -> None:
        self.server.requests.append(self)
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
    requests: list[BaseHTTPRequestHandler]
    errors: dict[str, tuple[int, dict[str, object]]]


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
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.content = video.read_bytes()
    httpd.contents = {"tail.mp4": tail.read_bytes()}
    httpd.requests = []
    httpd.errors = {}
    httpd.require_closed = None
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


def test_only_closed_ranges_reach_upstream(server: _Server, tmp_path: Path) -> None:
    """Drive refuses open-ended ranges under quota; the proxy still extracts.

    The served file has its moov atom at the end, so ffmpeg must seek — every
    upstream request still has to be a closed range of at most `chunk_bytes`.
    """
    chunk = 64 << 10
    server.require_closed = chunk
    out = extract_audio(
        [remote(server, "tail.mp4")], tmp_path / "s.flac",
        sleep=lambda _: None, chunk_bytes=chunk,
    )  # fmt: skip
    assert abs(probe(out).duration_ms - SECONDS * 1000) < 500
    assert server.requests
    for request in server.requests:
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("Range", ""))
        assert m, f"open-ended upstream request: {request.headers.get('Range')!r}"
        assert int(m[2]) - int(m[1]) + 1 <= chunk


def test_quota_on_closed_range_is_retryable(server: _Server, tmp_path: Path) -> None:
    """downloadQuotaExceeded on a closed range keeps its retryable retry_after."""
    server.require_closed = 1 << 20
    server.errors["tail.mp4"] = QUOTA_403
    with pytest.raises(MediaError) as exc:
        extract_audio([remote(server, "tail.mp4")], tmp_path / "s.flac",
                      attempts=1, sleep=lambda _: None)  # fmt: skip
    assert exc.value.retryable
    assert "downloadQuotaExceeded" in str(exc.value)
    assert exc.value.retry_after == ffmpeg.DEFAULT_QUOTA_RETRY_AFTER


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
