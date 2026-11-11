# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback range proxy: ffmpeg reads `RemoteMedia` through closed ranges only.

Drive's per-file download quota refuses open-ended requests (no `Range`, or
`Range: bytes=N-`) with 403 `downloadQuotaExceeded`, while closed ranges are
served fine — and ffmpeg's http protocol only ever sends open-ended ranges. Each
`RemoteMedia` is therefore served to ffmpeg by a short-lived local HTTP server:
ffmpeg reads `http://127.0.0.1:<port>/<token>` and the proxy translates every
request into consecutive closed upstream ranges, attaching the part's auth
headers (and refreshing them once on 401).

Quota costs Drive-side bytes, not just consumed ones, so upstream reads are
kept close to what ffmpeg actually consumes: a bounded prefetch window keeps
up to `prefetch` small closed ranges in flight for the next sequential chunks
(sequential reads are latency-bound, not bandwidth-bound), and an LRU cache of
aligned blocks serves re-reads across seeks without refetching. A
non-sequential read cancels the outstanding prefetches — the fetched-but-unread
bytes are counted and logged as discarded — so the window never runs ahead of
what the client asked for by more than `prefetch` x `PREFETCH_CHUNK_BYTES`. The
first upstream quota refusal latches: every later request gets 403 without
touching upstream.
"""

import json
import logging
import secrets
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from georg.core.types import MediaInput, RemoteMedia

log = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 64 << 20
"""Cap on the closed range a single upstream request may cover (64 MiB)."""

PREFETCH_CHUNK_BYTES = 8 << 20
"""Closed range each in-flight prefetch covers (8 MiB)."""

DEFAULT_PREFETCH = 4
"""Upstream range requests kept in flight per remote part."""

_BLOCK_BYTES = 4 << 20
"""Alignment of cached blocks."""

_CACHE_BYTES = 64 << 20
"""Total bytes the block cache holds."""

_COPY_BYTES = 1 << 20

_QUOTA_REASONS = frozenset({"downloadQuotaExceeded", "userRateLimitExceeded", "rateLimitExceeded"})


class _BlockCache:
    """LRU cache of fetched upstream bytes, keyed by aligned block.

    Each entry holds one contiguous fetched interval inside its block; a read
    that needs more of the block fetches only the missing tail and extends it.
    """

    def __init__(self, block_bytes: int = _BLOCK_BYTES, total_bytes: int = _CACHE_BYTES) -> None:
        self._block = block_bytes
        self._limit = total_bytes
        self._entries: OrderedDict[int, tuple[int, bytes]] = OrderedDict()
        self._bytes = 0

    def get(self, start: int, end: int) -> bytes:
        """The cached bytes covering `start..end`; call only inside coverage."""
        block = start // self._block
        entry_start, data = self._entries[block]
        self._entries.move_to_end(block)
        return data[start - entry_start : end - entry_start + 1]

    def covered_until(self, start: int) -> int | None:
        """Last cached offset contiguous with `start` in its block, or None."""
        entry = self._entries.get(start // self._block)
        if entry is None:
            return None
        entry_start, data = entry
        if not entry_start <= start < entry_start + len(data):
            return None
        self._entries.move_to_end(start // self._block)
        return entry_start + len(data) - 1

    def put(self, start: int, data: bytes) -> None:
        """Store fetched `data` at `start`, merging with an adjacent interval."""
        block = start // self._block
        entry = self._entries.get(block)
        if entry is not None and entry[0] + len(entry[1]) == start:
            data = entry[1] + data
            start = entry[0]
        if entry is not None:
            self._bytes -= len(entry[1])
        self._entries[block] = (start, data)
        self._entries.move_to_end(block)
        self._bytes += len(data)
        while self._bytes > self._limit and self._entries:
            _, (_, evicted) = self._entries.popitem(last=False)
            self._bytes -= len(evicted)


class _Fetch:
    """One in-flight upstream closed range, filled by a daemon thread.

    `data` grows as upstream bytes arrive; a consumer streams from it without
    waiting for the whole range. Bounded by the prefetcher's depth x chunk.
    """

    def __init__(self, start: int, end: int) -> None:
        self.start = start
        self.end = end
        self.status: int | None = None
        self.error: httpx.HTTPError | None = None
        self.data = bytearray()
        self.read = 0  # bytes handed to a client so far
        self.done = threading.Event()
        self.cancelled = False
        self.wake = threading.Condition()
        self.response: httpx.Response | None = None

    def wait_status(self) -> int | None:
        """The upstream status once response headers (or an error) arrive."""
        with self.wake:
            while self.status is None and self.error is None and not self.done.is_set():
                self.wake.wait()
            return self.status

    def stream_from(self, pos: int, end: int) -> Iterator[bytes]:
        """Yield the bytes of `pos..end` (inside `start..end`) as they arrive."""
        idx = pos - self.start
        last = min(end, self.end) - self.start + 1
        while idx < last:
            with self.wake:
                while len(self.data) <= idx and not self.done.is_set():
                    self.wake.wait()
                avail = min(len(self.data), last)
                if avail <= idx:
                    break
                out = bytes(self.data[idx:avail])
                self.read = max(self.read, avail)
                idx = avail
            yield out
        if self.error is not None:
            raise self.error


def _latch_if_quota(server: "_Proxy", response: httpx.Response) -> None:
    """Latch the proxy once upstream answers quota/rate-limit 403."""
    try:
        data = json.loads(response.read()[: 1 << 16])
        error = data.get("error") if isinstance(data, dict) else None
        reasons = {
            str(e.get("reason"))
            for e in (error or {}).get("errors", []) or []
            if isinstance(e, dict) and e.get("reason")
        }
    except (ValueError, AttributeError):
        reasons = set()
    if reasons & _QUOTA_REASONS:
        server.quota_latched = True


class _Prefetcher:
    """Bounded read-ahead: contiguous windows of closed upstream ranges.

    Each consumer gets a window of `depth` contiguous fetches starting at its
    read offset, so sequential readers overlap upstream latency instead of
    paying it per request. A read outside every in-flight range (ffmpeg
    seeking, or a second concurrent client request) opens a new window without
    disturbing the old one — windows a client abandons keep completing in the
    background and are adopted by whichever read covers them next, or evicted
    by a cap of `2 * depth` in-flight fetches, counting fetched-but-unread
    bytes as discarded.
    """

    def __init__(self, server: "_Proxy", *, depth: int, chunk: int) -> None:
        self._server = server
        self._depth = max(1, depth)
        self._chunk = chunk
        self._cap = 2 * self._depth
        self._lock = threading.Lock()
        self._fetches: OrderedDict[int, _Fetch] = OrderedDict()

    def open(self, start: int, end: int) -> httpx.Response:
        """One closed upstream range outside the prefetch window (size probes)."""
        response = self._send(start, end)
        if response.status_code == 401:
            response.close()
            response = self._send(start, end, force_refresh=True)
        return response

    def _send(self, start: int, end: int, *, force_refresh: bool = False) -> httpx.Response:
        refresh = force_refresh or self._server.force_refresh
        with self._lock:
            self._server.upstream_requests += 1
        request = self._server.client.build_request(
            "GET",
            self._server.part.url,
            headers={
                **self._server.part.headers(force_refresh=refresh),
                "Range": f"bytes={start}-{end}",
            },
        )
        return self._server.client.send(request, stream=True)

    def at(self, pos: int, limit: int) -> _Fetch | None:
        """The in-flight fetch covering `pos`, opening a window if none does.

        `limit` is the last byte the client asked for; fetches never pass EOF.
        """
        with self._lock:
            for start, fetch in list(self._fetches.items()):
                if fetch.done.is_set() and pos > fetch.end:
                    del self._fetches[start]
            covering = next(
                (f for f in self._fetches.values() if not f.cancelled and f.start <= pos <= f.end),
                None,
            )
            if covering is None and not self._server.quota_latched:
                start = pos
                for _ in range(self._depth):
                    if start > limit or start in self._fetches:
                        break
                    fetch = _Fetch(start, min(start + self._chunk - 1, limit))
                    self._fetches[start] = fetch
                    threading.Thread(target=self._run, args=(fetch,), daemon=True).start()
                    start = fetch.end + 1
                covering = self._fetches.get(pos)
            while len(self._fetches) > self._cap:
                evict = next((f for f in self._fetches.values() if f is not covering), None)
                if evict is None:
                    break
                self._discard(evict)
            return covering

    def _discard(self, fetch: _Fetch) -> None:
        """Cancel a fetch and count its fetched-but-unread bytes."""
        fetch.cancelled = True
        if fetch.response is not None:
            fetch.response.close()
        self._server.discarded_bytes += max(0, len(fetch.data) - fetch.read)
        self._fetches.pop(fetch.start, None)

    def drain(self) -> None:
        """Cancel everything still in flight (server shutdown)."""
        with self._lock:
            for fetch in list(self._fetches.values()):
                self._discard(fetch)

    def _run(self, fetch: _Fetch) -> None:
        try:
            response = self.open(fetch.start, fetch.end)
            fetch.response = response
            fetch.status = response.status_code
            with fetch.wake:
                fetch.wake.notify_all()
            if response.status_code == 403:
                _latch_if_quota(self._server, response)
            if response.status_code != 206:
                return
            for block in response.iter_bytes(_COPY_BYTES):
                if fetch.cancelled or self._server.quota_latched:
                    break
                with fetch.wake:
                    fetch.data += block
                    fetch.wake.notify_all()
                with self._lock:
                    self._server.upstream_bytes += len(block)
        except httpx.HTTPError as exc:
            fetch.error = exc
        finally:
            if fetch.response is not None:
                fetch.response.close()
            fetch.done.set()
            with fetch.wake:
                fetch.wake.notify_all()


class _Proxy(ThreadingHTTPServer):
    daemon_threads = True
    part: RemoteMedia
    chunk_bytes: int
    token: str
    client: httpx.Client
    size: int | None
    force_refresh: bool
    quota_latched: bool
    cache: _BlockCache
    prefetcher: _Prefetcher
    upstream_requests: int
    upstream_bytes: int
    served_bytes: int
    discarded_bytes: int


class _Forwarded(Exception):
    """An upstream failure ends the client request.

    `status` is relayed to the client while no body bytes have gone out yet;
    a mid-stream failure just closes the connection (ffmpeg's reconnect then
    hits the latch and receives the 403 cleanly).
    """

    def __init__(self, status: int | None = None) -> None:
        self.status = status


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
        if self.server.quota_latched:
            self._send_status(403)
            return
        try:
            total = self._size()
        except httpx.HTTPError:
            self.send_error(502, "upstream request failed")
            return
        except _Forwarded:
            return  # upstream error already relayed
        if total is None:
            self.send_error(502, "upstream size unknown")
            return
        start, end = self._client_range(total)
        if start >= total or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if head:
            self._send_data_headers(start, end)
            return
        # A client disconnect (ffmpeg seeking) or a mid-stream upstream failure
        # just ends this request; an error before the body still relays its
        # status. Upstream streams are closed in `finally`.
        try:
            self._stream(start, end)
        except _Forwarded as exc:
            if exc.status is not None:
                self._send_status(exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _client_range(self, total: int) -> tuple[int, int]:
        """The byte span ffmpeg asked for: open-ended `bytes=N-` or a plain GET."""
        header = self.headers.get("Range", "")
        spec = header.partition("=")[2]
        first, _, last = spec.partition("-")
        start = int(first) if first.isdigit() else 0
        end = int(last) if last.isdigit() else total - 1
        return start, min(end, total - 1)

    def _size(self) -> int | None:
        """Total source bytes: `RemoteMedia.size`, else a closed 1-byte probe."""
        if self.server.size is not None:
            return self.server.size
        response = self.server.prefetcher.open(0, 0)
        try:
            if response.status_code >= 400:
                if response.status_code == 403:
                    _latch_if_quota(self.server, response)
                self._send_status(response.status_code)
                raise _Forwarded
            tail = response.headers.get("Content-Range", "").partition("/")[2].strip()
            self.server.size = int(tail) if tail.isdigit() else None
            return self.server.size
        finally:
            response.close()

    def _piece(self, start: int, end: int, limit: int, *, head: bool) -> Iterator[bytes]:
        """Yield `start..end` of the source, from the block cache or prefetch.

        Cached prefixes are served without touching upstream; a miss consumes
        the prefetch window — contiguous closed ranges fetched `depth`-deep —
        and caches bytes as they pass through, so a client that disconnects
        early (ffmpeg seeking away) leaves bounded waste. `head` is true while
        no body bytes have been sent: a failure can still relay its status.
        """
        pos = start
        while pos <= end:
            if self.server.quota_latched:
                raise _Forwarded(403 if head else None)
            hit_end = self.server.cache.covered_until(pos)
            if hit_end is not None:
                take = min(hit_end, end)
                yield self.server.cache.get(pos, take)
                pos = take + 1
                continue
            fetch = self.server.prefetcher.at(pos, limit)
            if fetch is None:
                break
            status = fetch.wait_status()
            if self.server.quota_latched:
                raise _Forwarded(403 if head else None)
            if fetch.error is not None:
                raise fetch.error
            if status != 206:
                raise _Forwarded(status if head else None)
            got = 0
            try:
                for data in fetch.stream_from(pos, end):
                    self.server.cache.put(pos, data)
                    pos += len(data)
                    got += len(data)
                    yield data
            except httpx.HTTPError:
                if self.server.quota_latched:
                    raise _Forwarded(403 if head else None) from None
                raise
            if got == 0:
                break  # upstream ended early; stop rather than refetch

    def _stream(self, start: int, end: int) -> None:
        """Copy `start..end` to the client, prefetching upstream sequentially.

        Pieces stay inside one aligned block so cache entries merge; the
        prefetcher keeps `depth` closed ranges in flight behind the read
        position, and a seek discards the window instead of over-fetching.
        The 206 headers go out with the first body bytes, so a failure on the
        first piece can still relay its upstream status.
        """
        sent = False
        pos = start
        while pos <= end:
            block_end = (pos // _BLOCK_BYTES + 1) * _BLOCK_BYTES - 1
            piece_end = min(block_end, end)
            for data in self._piece(pos, piece_end, end, head=not sent):
                if not sent:
                    self._send_data_headers(start, end)
                    sent = True
                self.wfile.write(data)
                self.server.served_bytes += len(data)
            pos = piece_end + 1

    def _send_data_headers(self, start: int, end: int) -> None:
        """The 200/206 response headers, sent with the first body bytes."""
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
def serve_remote(
    part: RemoteMedia,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    prefetch: int = DEFAULT_PREFETCH,
    force_refresh: bool = False,
) -> Iterator[str]:
    """Serve `part` to ffmpeg on loopback for the duration of the block.

    Yields the local URL. The server only answers an unguessable path, fetches
    upstream in closed ranges of at most `min(chunk_bytes,
    PREFETCH_CHUNK_BYTES)` kept `prefetch`-deep, and logs its upstream traffic
    when it stops.
    """
    server = _Proxy(("127.0.0.1", 0), _Handler)
    server.part = part
    server.chunk_bytes = chunk_bytes
    server.token = secrets.token_hex(16)
    server.size = part.size
    server.force_refresh = force_refresh
    server.quota_latched = False
    server.cache = _BlockCache()
    server.upstream_requests = 0
    server.upstream_bytes = 0
    server.served_bytes = 0
    server.discarded_bytes = 0
    server.client = httpx.Client(timeout=httpx.Timeout(None, connect=30.0), follow_redirects=True)
    server.prefetcher = _Prefetcher(
        server, depth=prefetch, chunk=min(PREFETCH_CHUNK_BYTES, chunk_bytes)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{server.token}"
    finally:
        server.prefetcher.drain()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        server.client.close()
        log.info(
            "%s: %d upstream requests, %d upstream bytes, %d bytes served"
            ", %d prefetched bytes discarded",
            part.name,
            server.upstream_requests,
            server.upstream_bytes,
            server.served_bytes,
            server.discarded_bytes,
        )


@contextmanager
def serve_all(
    parts: list[MediaInput],
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    prefetch: int = DEFAULT_PREFETCH,
    force_refresh: bool = False,
) -> Iterator[dict[int, str]]:
    """Serve every `RemoteMedia` in `parts`; yields `{id(part): local url}`."""
    with ExitStack() as stack:
        urls = {
            id(part): stack.enter_context(
                serve_remote(
                    part,
                    chunk_bytes=chunk_bytes,
                    prefetch=prefetch,
                    force_refresh=force_refresh,
                )
            )
            for part in parts
            if isinstance(part, RemoteMedia)
        }
        yield urls
