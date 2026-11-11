# SPDX-License-Identifier: AGPL-3.0-only
"""Serve remote media through one sequential upstream stream.

The local HTTP endpoint accepts ffmpeg's range requests, but the upstream
side reads the file front to back. Forward seeks discard bytes from that
stream; backward seeks reopen at the requested offset and are logged. The
MP4 tail is fetched once as a small closed range and served from memory.
"""

import json
import logging
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from georg.core.types import MediaInput, RemoteMedia

log = logging.getLogger(__name__)

TAIL_BYTES = 8 << 20
RECENT_BYTES = 16 << 20
RESUME_ATTEMPTS = 5
_COPY_BYTES = 1 << 20
_QUOTA_REASONS = frozenset({"downloadQuotaExceeded", "userRateLimitExceeded", "rateLimitExceeded"})


class _Forwarded(Exception):
    def __init__(self, status: int | None = None) -> None:
        self.status = status


def _latch_if_quota(server: "_Proxy", response: httpx.Response) -> None:
    try:
        data = json.loads(response.read()[: 1 << 16])
        error = data.get("error") if isinstance(data, dict) else None
        reasons = {
            str(item.get("reason"))
            for item in (error or {}).get("errors", []) or []
            if isinstance(item, dict) and item.get("reason")
        }
    except (ValueError, AttributeError):
        reasons = set()
    if reasons & _QUOTA_REASONS:
        server.quota_latched = True


class _Upstream:
    def __init__(self, server: "_Proxy") -> None:
        self.server = server

    def open(self, start: int, end: int) -> httpx.Response:
        response = self._send(start, end)
        if response.status_code == 401:
            response.close()
            response = self._send(start, end, force_refresh=True)
        return response

    def _send(self, start: int, end: int, *, force_refresh: bool = False) -> httpx.Response:
        self.server.upstream_requests += 1
        headers = {
            **self.server.part.headers(force_refresh=force_refresh or self.server.force_refresh),
            "Range": f"bytes={start}-{end}",
        }
        request = self.server.client.build_request("GET", self.server.part.url, headers=headers)
        return self.server.client.send(request, stream=True)


class _SequentialBody:
    """A resumable, front-to-back stream shared by all local requests."""

    def __init__(self, server: "_Proxy", end: int) -> None:
        self.server = server
        self.end = end
        self.lock = threading.RLock()
        self.upstream = _Upstream(server)
        self.response: httpx.Response | None = None
        self.chunks: Iterator[bytes] | None = None
        self.pos = 0
        self.recent = bytearray()
        self.recent_start = 0

    def close(self) -> None:
        with self.lock:
            self._close()

    def read(self, start: int, end: int, *, head: bool) -> Iterator[bytes]:
        """Yield `start..end` of the body, seeking inside the stream as needed.

        `self.pos` is the next byte upstream will deliver; `_pushback` holds
        fetched-ahead bytes not yet handed out, so the byte a sequential
        consumer wants next is `self.pos - len(_pushback)`, and `recent`
        covers `[recent_start, self.pos)` for backward replay.
        """
        with self.lock:
            self._pushback = getattr(self, "_pushback", b"")
            if start < self.pos - len(self._pushback):
                if start < self.recent_start:
                    self.server.backward_seeks += 1
                    log.info(
                        "%s: backward seek to byte %d (stream at %d)",
                        self.server.part.name,
                        start,
                        self.pos,
                    )
                    self._close()
                    self.pos = start
                    self._pushback = b""
                    self.recent.clear()
                    self.recent_start = start
                else:
                    replay_end = min(end + 1, self.pos)
                    yield bytes(
                        self.recent[start - self.recent_start : replay_end - self.recent_start]
                    )
                    self._pushback = b""
                    start = replay_end
                    if start > end:
                        return
            while self.pos - len(self._pushback) < start:
                frontier = self.pos - len(self._pushback)
                piece = self._next(head=head)
                skip = min(len(piece), start - frontier)
                if skip < len(piece):
                    self._pushback = piece[skip:]
            while self.pos - len(self._pushback) <= end:
                frontier = self.pos - len(self._pushback)
                piece = self._next(head=head)
                take = min(len(piece), end - frontier + 1)
                if take:
                    yield piece[:take]
                if take < len(piece):
                    self._pushback = piece[take:]
                    break

    def _next(self, *, head: bool) -> bytes:
        pushback = getattr(self, "_pushback", b"")
        if pushback:
            self._pushback = b""
            return pushback
        attempts = 0
        last_error: httpx.HTTPError | None = None
        while True:
            self._ensure(head=head)
            assert self.chunks is not None
            try:
                piece = next(self.chunks)
            except StopIteration:
                self._close()
                if self.pos > self.end:
                    raise _Forwarded() from None
                piece = b""
            except httpx.HTTPError as exc:
                last_error = exc
                piece = b""
                self._close()
            if piece:
                self._record(piece)
                return piece
            attempts += 1
            if attempts > RESUME_ATTEMPTS:
                raise _Forwarded() from last_error
            self.server.resumes += 1
            time.sleep(min(0.5 * 2 ** (attempts - 1), 8))

    def _ensure(self, *, head: bool) -> None:
        if self.response is not None:
            return
        if self.server.quota_latched:
            raise _Forwarded(403 if head else None)
        response = self.upstream.open(self.pos, self.end)
        if response.status_code == 403:
            _latch_if_quota(self.server, response)
        if response.status_code != 206:
            status = response.status_code
            response.close()
            raise _Forwarded(status if head else None)
        self.response = response
        self.chunks = response.iter_bytes(_COPY_BYTES)

    def _record(self, piece: bytes) -> None:
        self.server.upstream_bytes += len(piece)
        self.pos += len(piece)
        self.recent.extend(piece)
        if len(self.recent) > RECENT_BYTES:
            drop = len(self.recent) - RECENT_BYTES
            del self.recent[:drop]
            self.recent_start += drop

    def _close(self) -> None:
        if self.response is not None:
            self.response.close()
        self.response = None
        self.chunks = None


class _Proxy(ThreadingHTTPServer):
    daemon_threads = True
    part: RemoteMedia
    token: str
    client: httpx.Client
    size: int | None
    force_refresh: bool
    quota_latched: bool
    upstream_requests: int
    upstream_bytes: int
    served_bytes: int
    resumes: int
    backward_seeks: int
    tail_start: int
    tail: bytes
    upstream: _Upstream
    body: _SequentialBody
    initialized: bool


class _Handler(BaseHTTPRequestHandler):
    server: _Proxy
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_HEAD(self) -> None:
        self._handle(head=True)

    def do_GET(self) -> None:
        self._handle(head=False)

    def _handle(self, *, head: bool) -> None:
        if self.path != f"/{self.server.token}":
            self.send_error(404)
            return
        try:
            total = self._size()
            self._ensure_streams()
            start, end = self._client_range(total)
            if start >= total or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if head:
                self._send_headers(start, end)
                return
            sent = False
            for data in self._read(start, end):
                if not sent:
                    self._send_headers(start, end)
                    sent = True
                self.wfile.write(data)
                self.server.served_bytes += len(data)
        except _Forwarded as exc:
            if exc.status is not None:
                self._send_status(exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except httpx.HTTPError:
            if not self.server.quota_latched:
                self._send_status(502)

    def _size(self) -> int:
        if self.server.size is not None:
            return self.server.size
        response = self.server.upstream.open(0, 0)
        try:
            if response.status_code == 403:
                _latch_if_quota(self.server, response)
            if response.status_code >= 400:
                raise _Forwarded(response.status_code)
            value = response.headers.get("Content-Range", "").rpartition("/")[2]
            if not value.isdigit():
                raise _Forwarded(502)
            self.server.size = int(value)
            return self.server.size
        finally:
            response.close()

    def _ensure_streams(self) -> None:
        if not self.server.initialized:
            _init_streams(self.server)
            self.server.initialized = True

    def _client_range(self, total: int) -> tuple[int, int]:
        spec = self.headers.get("Range", "").partition("=")[2]
        first, _, last = spec.partition("-")
        start = int(first) if first.isdigit() else 0
        end = int(last) if last.isdigit() else total - 1
        return start, min(end, total - 1)

    def _read(self, start: int, end: int) -> Iterator[bytes]:
        tail_start = self.server.tail_start
        if start < tail_start:
            body_end = min(end, tail_start - 1)
            yield from self.server.body.read(start, body_end, head=True)
        if end >= tail_start:
            assert self.server.size is not None
            tail_begin = max(start, tail_start)
            tail_end = min(end, self.server.size - 1)
            yield self.server.tail[tail_begin - tail_start : tail_end - tail_start + 1]

    def _send_headers(self, start: int, end: int) -> None:
        self.send_response(206 if "Range" in self.headers else 200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if "Range" in self.headers:
            self.send_header("Content-Range", f"bytes {start}-{end}/{self.server.size}")
        self.end_headers()

    def _send_status(self, status: int) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def serve_remote(part: RemoteMedia, *, force_refresh: bool = False) -> Iterator[str]:
    """Serve one remote file through a single resumable upstream stream."""
    server = _Proxy(("127.0.0.1", 0), _Handler)
    server.part = part
    server.token = secrets.token_hex(16)
    server.size = part.size
    server.force_refresh = force_refresh
    server.quota_latched = False
    server.upstream_requests = 0
    server.upstream_bytes = 0
    server.served_bytes = 0
    server.resumes = 0
    server.backward_seeks = 0
    server.client = httpx.Client(timeout=httpx.Timeout(None, connect=30.0), follow_redirects=True)
    server.upstream = _Upstream(server)
    server.tail = b""
    server.tail_start = 0
    server.body = _SequentialBody(server, 0)
    server.initialized = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{server.token}"
    finally:
        server.body.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        server.client.close()
        log.info(
            "%s: %d upstream requests, %d upstream bytes, %d bytes served, "
            "%d resumes, %d backward seeks",
            part.name,
            server.upstream_requests,
            server.upstream_bytes,
            server.served_bytes,
            server.resumes,
            server.backward_seeks,
        )


def _init_streams(server: _Proxy) -> None:
    assert server.size is not None
    server.tail_start = max(0, server.size - TAIL_BYTES)
    server.body = _SequentialBody(server, server.tail_start - 1)
    response = server.upstream.open(server.tail_start, server.size - 1)
    try:
        if response.status_code == 403:
            _latch_if_quota(server, response)
        if response.status_code != 206:
            raise _Forwarded(response.status_code)
        server.tail = response.read()
        server.upstream_bytes += len(server.tail)
    finally:
        response.close()


@contextmanager
def serve_all(parts: list[MediaInput], *, force_refresh: bool = False) -> Iterator[dict[int, str]]:
    """Serve every `RemoteMedia` in `parts`; yields `{id(part): local url}`."""
    with ExitStack() as stack:
        urls = {
            id(part): stack.enter_context(serve_remote(part, force_refresh=force_refresh))
            for part in parts
            if isinstance(part, RemoteMedia)
        }
        yield urls
