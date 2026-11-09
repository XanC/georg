# SPDX-License-Identifier: AGPL-3.0-only
"""`extract_audio`/`probe_media` on `RemoteMedia` inputs, against a local HTTP server.

The server requires a bearer token and honors `Range`, like Drive's `alt=media`
endpoint; no real Google calls or credentials are involved.
"""

import re
import shutil
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_ffmpeg import tone_video

from georg.core.types import RemoteMedia
from georg.media import MediaError, extract_audio, probe, probe_media

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.server.token}"

    def _send(self, start: int, end: int, *, head: bool = False) -> None:
        data = self.server.content
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
        start, end = 0, len(self.server.content) - 1
        if m := re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "")):
            start = int(m[1])
            if m[2]:
                end = int(m[2])
        self._send(start, end, head=head)

    def do_GET(self) -> None:
        self._handle(head=False)

    def do_HEAD(self) -> None:
        self._handle(head=True)

    def log_message(self, *args: object) -> None:
        pass


class _Server(ThreadingHTTPServer):
    token = "good-token"
    content: bytes
    requests: list[BaseHTTPRequestHandler]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_Server]:
    content = tone_video(tmp_path / "part.mp4", 2).read_bytes()
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.content = content
    httpd.requests = []
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
    assert abs(info.duration_ms - 2000) < 300
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
    assert abs(probe(out).duration_ms - 4000) < 400
