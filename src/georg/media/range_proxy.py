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
kept close to what ffmpeg actually consumes: each client request starts with a
small upstream range that doubles while the client keeps reading sequentially
(capped at `chunk_bytes`), plus an LRU cache of aligned blocks so re-reads
across seeks don't refetch. The first upstream quota refusal latches: every
later request gets 403 without touching upstream.
"""

import json
import logging
import secrets
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from georg.core.types import MediaInput, RemoteMedia

log = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 64 << 20
"""Largest single closed range requested upstream (64 MiB)."""

INITIAL_CHUNK_BYTES = 1 << 20
"""Upstream range a client request starts with; doubles while it reads on."""

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
    upstream_requests: int
    upstream_bytes: int
    served_bytes: int


class _Forwarded(Exception):
    """An upstream error already relayed to the client."""


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
        self.send_response(206 if "Range" in self.headers else 200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if "Range" in self.headers:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if head:
            return
        # A client disconnect (ffmpeg seeking) or a relayed upstream error just
        # ends this request; the upstream stream is closed in `finally`.
        with suppress(BrokenPipeError, ConnectionResetError, _Forwarded):
            self._stream(start, end)

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
        response = self._fetch(0, 0)
        try:
            if response.status_code >= 400:
                if response.status_code == 403:
                    self._latch_if_quota(response)
                self._forward_error(response)
                raise _Forwarded
            tail = response.headers.get("Content-Range", "").partition("/")[2].strip()
            self.server.size = int(tail) if tail.isdigit() else None
            return self.server.size
        finally:
            response.close()

    def _upstream(self, start: int, end: int, *, force_refresh: bool = False) -> httpx.Response:
        """Open a streamed upstream GET for one closed range."""
        refresh = force_refresh or self.server.force_refresh
        self.server.upstream_requests += 1
        request = self.server.client.build_request(
            "GET",
            self.server.part.url,
            headers={
                **self.server.part.headers(force_refresh=refresh),
                "Range": f"bytes={start}-{end}",
            },
        )
        return self.server.client.send(request, stream=True)

    def _fetch(self, start: int, end: int) -> httpx.Response:
        """One closed upstream range, refreshing credentials once on 401."""
        response = self._upstream(start, end)
        if response.status_code == 401:
            response.close()
            response = self._upstream(start, end, force_refresh=True)
        return response

    def _piece(self, start: int, end: int) -> Iterator[bytes]:
        """Yield `start..end` of the source, from the block cache or upstream.

        Cached prefixes are served without touching upstream; a miss is fetched
        as one upstream range streamed through in small blocks and cached as it
        arrives, so a client that disconnects early (ffmpeg seeking away) stops
        the upstream read instead of pulling the whole range.
        """
        pos = start
        while pos <= end:
            hit_end = self.server.cache.covered_until(pos)
            if hit_end is not None:
                take = min(hit_end, end)
                yield self.server.cache.get(pos, take)
                pos = take + 1
                continue
            response = self._fetch(pos, end)
            got = 0
            try:
                if response.status_code == 403:
                    self._latch_if_quota(response)
                    self._forward_error(response)
                    raise _Forwarded
                if response.status_code != 206:
                    self._forward_error(response)
                    raise _Forwarded
                for block in response.iter_bytes(_COPY_BYTES):
                    self.server.upstream_bytes += len(block)
                    self.server.cache.put(pos, block)
                    pos += len(block)
                    got += len(block)
                    yield block
            finally:
                response.close()
            if got == 0:
                break  # upstream ended early; stop rather than refetch

    def _latch_if_quota(self, response: httpx.Response) -> None:
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
            self.server.quota_latched = True

    def _stream(self, start: int, end: int) -> None:
        """Copy `start..end` to the client with adaptive upstream read-ahead.

        Each client request begins with a small upstream range; while the client
        keeps reading sequentially the range doubles up to `chunk_bytes`, and
        cache hits serve seeks without any upstream traffic.
        """
        pos = start
        chunk = min(INITIAL_CHUNK_BYTES, self.server.chunk_bytes)
        while pos <= end:
            # Pieces stay inside one aligned block so cache entries merge.
            block_end = (pos // _BLOCK_BYTES + 1) * _BLOCK_BYTES - 1
            piece_end = min(pos + chunk - 1, block_end, end)
            for data in self._piece(pos, piece_end):
                self.wfile.write(data)
                self.server.served_bytes += len(data)
            pos = piece_end + 1
            chunk = min(chunk * 2, self.server.chunk_bytes)

    def _forward_error(self, response: httpx.Response) -> None:
        """Relay an upstream status to ffmpeg so its stderr names it."""
        self._send_status(response.status_code)

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
    force_refresh: bool = False,
) -> Iterator[str]:
    """Serve `part` to ffmpeg on loopback for the duration of the block.

    Yields the local URL. The server only answers an unguessable path, fetches
    upstream in closed ranges, and logs its upstream traffic when it stops.
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
    server.client = httpx.Client(timeout=httpx.Timeout(None, connect=30.0), follow_redirects=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{server.token}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        server.client.close()
        log.info(
            "%s: %d upstream requests, %d upstream bytes, %d bytes served",
            part.name,
            server.upstream_requests,
            server.upstream_bytes,
            server.served_bytes,
        )


@contextmanager
def serve_all(
    parts: list[MediaInput],
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    force_refresh: bool = False,
) -> Iterator[dict[int, str]]:
    """Serve every `RemoteMedia` in `parts`; yields `{id(part): local url}`."""
    with ExitStack() as stack:
        urls = {
            id(part): stack.enter_context(
                serve_remote(part, chunk_bytes=chunk_bytes, force_refresh=force_refresh)
            )
            for part in parts
            if isinstance(part, RemoteMedia)
        }
        yield urls
