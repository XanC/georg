# SPDX-License-Identifier: AGPL-3.0-only
"""Serve remote media through one sequential upstream stream.

The local HTTP endpoint accepts ffmpeg's range requests, but the upstream
side reads the file front to back. Forward seeks discard bytes from that
stream; backward seeks replay a recent-bytes buffer or, before it, reopen
at the requested offset (logged). The MP4 tail is fetched once as a closed
range and served from memory, extending backwards by up to `TAIL_BYTES`
per fetch when a moov read starts earlier.

ffmpeg keeps an old connection open while its new request waits, so a new
client request always preempts: the previous client's socket is closed and
the shared stream is repositioned for the new request — otherwise a stalled
write on the old connection would deadlock the new one. If neither side
makes progress for `stall_seconds` the attempt fails instead of hanging.
"""

import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from georg.core.types import MediaInput, RemoteMedia

log = logging.getLogger(__name__)

TAIL_BYTES = 16 << 20
"""Bytes per closed-range fetch for the MP4 index at the end of the file."""

RECENT_BYTES = 16 << 20
"""How much of the stream is replayable for a backward seek."""

RESUME_ATTEMPTS = 5
"""How often a dropped upstream connection is resumed."""

STALL_SECONDS = 300.0
"""How long either side may go without progress before the attempt fails."""

_COPY_BYTES = 1 << 20

_QUOTA_REASONS = frozenset({"downloadQuotaExceeded", "userRateLimitExceeded", "rateLimitExceeded"})


class _Forwarded(Exception):
    """An upstream failure ends the client request.

    `status` is relayed to the client while no body bytes have gone out yet;
    a mid-stream failure just closes the connection (ffmpeg's reconnect then
    hits the latch and receives the 403 cleanly).
    """

    def __init__(self, status: int | None = None) -> None:
        self.status = status


def _latch_if_quota(server: "_Proxy", response: httpx.Response) -> None:
    """Latch the proxy once upstream answers quota/rate-limit 403."""
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
    """Sends `Range` requests against the part's URL, counting traffic."""

    def __init__(self, server: "_Proxy") -> None:
        self.server = server

    def open(self, start: int, end: int) -> httpx.Response:
        """One closed upstream range; 401 retries once with fresh headers."""
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
    """A resumable, front-to-back stream shared by all local requests.

    `pos` is the next byte upstream will deliver; `_pushback` holds
    fetched-ahead bytes not yet handed out, so the byte a sequential
    consumer wants next is `pos - len(_pushback)`, and `recent` covers
    `[recent_start, pos)` for backward replay.

    Only one client request streams at a time, but a new request never
    waits: `_preempt` closes the previous client's socket so its handler's
    blocked write errors out and releases the lock.
    """

    def __init__(self, server: "_Proxy") -> None:
        self.server = server
        self.lock = threading.Lock()
        self._gate = threading.Lock()
        self._owner: socket.socket | None = None
        self._preempted = False
        self.upstream = _Upstream(server)
        self.response: httpx.Response | None = None
        self.chunks: Iterator[bytes] | None = None
        self.pos = 0
        self.recent = bytearray()
        self.recent_start = 0
        self._pushback = b""

    def close(self) -> None:
        with self.lock:
            self._close()

    def _preempt(self, client: socket.socket) -> None:
        """End the current owner's client connection so it frees the lock."""
        with self._gate:
            self._preempted = True
            previous = self._owner
            self._owner = client
        if previous is not None and previous is not client:
            self.server.preempts += 1
            log.debug("%s: new client request preempts the previous one", self.server.part.name)
            with suppress(OSError):
                previous.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                previous.close()

    def read(self, start: int, end: int, *, head: bool, client: socket.socket) -> Iterator[bytes]:
        """Yield `start..end` of the body, seeking inside the stream as needed."""
        self._preempt(client)
        with self.lock:
            with self._gate:
                self._preempted = False
            try:
                yield from self._read(start, end, head=head)
            finally:
                with self._gate:
                    if self._owner is client:
                        self._owner = None

    def _read(self, start: int, end: int, *, head: bool) -> Iterator[bytes]:
        body_end = min(end, self.server.tail_view[0] - 1)
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
                replay_end = min(body_end + 1, self.pos)
                yield bytes(self.recent[start - self.recent_start : replay_end - self.recent_start])
                self._pushback = b""
                start = replay_end
                if start > body_end:
                    return
        while self.pos - len(self._pushback) < start:
            frontier = self.pos - len(self._pushback)
            piece = self._next(head=head)
            skip = min(len(piece), start - frontier)
            if skip < len(piece):
                self._pushback = piece[skip:]
        while self.pos - len(self._pushback) <= body_end:
            with self._gate:
                if self._preempted:
                    return
            frontier = self.pos - len(self._pushback)
            piece = self._next(head=head)
            take = min(len(piece), body_end - frontier + 1)
            if take:
                yield piece[:take]
            if take < len(piece):
                self._pushback = piece[take:]
                break

    def _next(self, *, head: bool) -> bytes:
        if self._pushback:
            piece, self._pushback = self._pushback, b""
            return piece
        attempts = 0
        last_error: httpx.HTTPError | None = None
        while True:
            self._ensure(head=head)
            assert self.chunks is not None
            try:
                piece = next(self.chunks)
            except StopIteration:
                self._close()
                if self.pos > self._end():
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
            with self._gate:
                if self._preempted:
                    raise _Forwarded() from None
            self.server.resumes += 1
            time.sleep(min(0.5 * 2 ** (attempts - 1), 8))

    def _end(self) -> int:
        """Last byte the body stream covers (the tail cache owns the rest)."""
        return self.server.tail_view[0] - 1

    def _ensure(self, *, head: bool) -> None:
        if self.response is not None:
            return
        if self.server.quota_latched:
            raise _Forwarded(403 if head else None)
        response = self.upstream.open(self.pos, self._end())
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
    preempts: int
    backward_seeks: int
    tail_view: tuple[int, bytes]
    tail_lock: threading.Lock
    upstream: _Upstream
    body: _SequentialBody
    init_lock: threading.Lock
    initialized: bool
    stall_seconds: float
    send_buffer: int | None


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
            # A blocked client write or a stalled upstream fails the attempt
            # after `stall_seconds` instead of hanging the extraction forever.
            self.connection.settimeout(self.server.stall_seconds)
            if self.server.send_buffer:
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_SNDBUF, self.server.send_buffer
                )
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
        except TimeoutError:
            log.info(
                "%s: no progress for %.0f s; closing the connection",
                self.server.part.name,
                self.server.stall_seconds,
            )
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
            with self.server.init_lock:
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
        """Yield `start..end`: body from the shared stream, tail from memory."""
        server = self.server
        tail_start, tail = server.tail_view
        if (
            0 < start < tail_start
            and end >= tail_start
            and start >= tail_start - TAIL_BYTES
            and start >= server.body.pos - len(server.body._pushback)
        ):
            # A read reaching into the tail region may be ffmpeg's moov fetch;
            # if it begins just before the cached tail, extend the tail cache
            # backwards instead of pushing the read through the body stream.
            # Bytes the body stream already passed replay from `recent` instead.
            tail_start, tail = _extend_tail(server, start)
        if start < tail_start:
            yield from server.body.read(
                start, min(end, tail_start - 1), head=True, client=self.connection
            )
        tail_start, tail = server.tail_view
        if end >= tail_start:
            assert server.size is not None
            tail_begin = max(start, tail_start)
            tail_end = min(end, server.size - 1)
            yield tail[tail_begin - tail_start : tail_end - tail_start + 1]

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


def _extend_tail(server: _Proxy, start: int) -> tuple[int, bytes]:
    """Grow the cached tail backwards with closed ranges until it covers `start`."""
    with server.tail_lock:
        tail_start, tail = server.tail_view
        while start < tail_start:
            fetch_start = max(start, tail_start - TAIL_BYTES)
            response = server.upstream.open(fetch_start, tail_start - 1)
            try:
                if response.status_code == 403:
                    _latch_if_quota(server, response)
                if response.status_code != 206:
                    raise _Forwarded(response.status_code)
                piece = response.read()
                server.upstream_bytes += len(piece)
            finally:
                response.close()
            tail_start, tail = fetch_start, piece + tail
            server.tail_view = (tail_start, tail)
        return server.tail_view


@contextmanager
def serve_remote(
    part: RemoteMedia,
    *,
    force_refresh: bool = False,
    stall_seconds: float = STALL_SECONDS,
    send_buffer: int | None = None,
) -> Iterator[str]:
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
    server.preempts = 0
    server.backward_seeks = 0
    server.client = httpx.Client(
        timeout=httpx.Timeout(None, connect=30.0, read=stall_seconds),
        follow_redirects=True,
    )
    server.upstream = _Upstream(server)
    server.tail_view = (0, b"")
    server.tail_lock = threading.Lock()
    server.body = _SequentialBody(server)
    server.init_lock = threading.Lock()
    server.initialized = False
    server.stall_seconds = stall_seconds
    server.send_buffer = send_buffer
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
            "%d resumes, %d preempts, %d backward seeks",
            part.name,
            server.upstream_requests,
            server.upstream_bytes,
            server.served_bytes,
            server.resumes,
            server.preempts,
            server.backward_seeks,
        )


def _init_streams(server: _Proxy) -> None:
    """Fetch the file's last `TAIL_BYTES` once; the body stream stays lazy."""
    assert server.size is not None
    tail_start = max(0, server.size - TAIL_BYTES)
    response = server.upstream.open(tail_start, server.size - 1)
    try:
        if response.status_code == 403:
            _latch_if_quota(server, response)
        if response.status_code != 206:
            raise _Forwarded(response.status_code)
        tail = response.read()
        server.upstream_bytes += len(tail)
        server.tail_view = (tail_start, tail)
    finally:
        response.close()


@contextmanager
def serve_all(
    parts: list[MediaInput],
    *,
    force_refresh: bool = False,
    stall_seconds: float = STALL_SECONDS,
) -> Iterator[dict[int, str]]:
    """Serve every `RemoteMedia` in `parts`; yields `{id(part): local url}`."""
    with ExitStack() as stack:
        urls = {
            id(part): stack.enter_context(
                serve_remote(part, force_refresh=force_refresh, stall_seconds=stall_seconds)
            )
            for part in parts
            if isinstance(part, RemoteMedia)
        }
        yield urls
