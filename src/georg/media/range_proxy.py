# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback range proxy: ffmpeg reads `RemoteMedia` through closed ranges only.

Drive's per-file download quota refuses open-ended requests (no `Range`, or
`Range: bytes=N-`) with 403 `downloadQuotaExceeded`, while closed ranges are
served fine — and ffmpeg's http protocol only ever sends open-ended ranges. Each
`RemoteMedia` is therefore served to ffmpeg by a short-lived local HTTP server:
ffmpeg reads `http://127.0.0.1:<port>/<token>` and the proxy translates every
request into consecutive closed upstream ranges of at most `chunk_bytes`,
attaching the part's auth headers (and refreshing them once on 401).
"""

import secrets
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from georg.core.types import MediaInput, RemoteMedia

DEFAULT_CHUNK_BYTES = 256 << 20
"""Largest single closed range requested upstream (256 MiB)."""

_COPY_BYTES = 1 << 20


class _Proxy(ThreadingHTTPServer):
    daemon_threads = True
    part: RemoteMedia
    chunk_bytes: int
    token: str
    client: httpx.Client
    size: int | None
    force_refresh: bool


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
        # ends this request; the upstream stream is closed by its context manager.
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

    def _stream(self, start: int, end: int) -> None:
        """Copy `start..end` to the client in closed `chunk_bytes` upstream reads."""
        pos = start
        while pos <= end:
            chunk_end = min(pos + self.server.chunk_bytes - 1, end)
            response = self._fetch(pos, chunk_end)
            try:
                if response.status_code != 206:
                    self._forward_error(response)
                    raise _Forwarded
                for block in response.iter_bytes(_COPY_BYTES):
                    self.wfile.write(block)
            finally:
                response.close()
            pos = chunk_end + 1

    def _forward_error(self, response: httpx.Response) -> None:
        """Relay an upstream status to ffmpeg so its stderr names it."""
        try:
            self.send_response(response.status_code)
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

    Yields the local URL. The server only answers an unguessable path and fetches
    upstream in closed ranges of at most `chunk_bytes`.
    """
    server = _Proxy(("127.0.0.1", 0), _Handler)
    server.part = part
    server.chunk_bytes = chunk_bytes
    server.token = secrets.token_hex(16)
    server.size = part.size
    server.force_refresh = force_refresh
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
