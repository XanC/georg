# SPDX-License-Identifier: AGPL-3.0-only
"""HMAC-SHA256 request signing for the `georg-bridge` WordPress plugin.

The protocol is specified in docs/wordpress.md; `wordpress/georg-bridge/includes/signing.php`
is the server side, and `wordpress/georg-bridge/tests/vectors.json` pins both to the same
canonical strings and signatures.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable, Generator, Iterator, Mapping
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

ALGORITHM = "GEORG-HMAC-SHA256"
MIN_SECRET_BYTES = 32
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
QUERY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK_BYTES = 1 << 20

HEADER_KEY_ID = "X-Georg-Key-Id"
HEADER_TIMESTAMP = "X-Georg-Timestamp"
HEADER_NONCE = "X-Georg-Nonce"
HEADER_CONTENT_SHA256 = "X-Georg-Content-SHA256"
HEADER_SIGNATURE = "X-Georg-Signature"


def decode_secret(text: str) -> bytes:
    """Decode a base64url (padding optional) secret of at least 32 bytes."""
    stripped = text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", stripped):
        raise ValueError("key is not base64url")
    try:
        plain = stripped.rstrip("=")
        secret = base64.urlsafe_b64decode(plain + "=" * (-len(plain) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("key is not base64url") from None
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"key must decode to at least {MIN_SECRET_BYTES} bytes")
    return secret


def canonical_query(query: Mapping[str, str]) -> str:
    """Sorted `rawurlencode(k)=rawurlencode(v)` pairs joined by "&", `rest_route` excluded."""
    pairs = sorted((k.encode(), v.encode()) for k, v in query.items() if k != "rest_route")
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)


def canonical_string(
    *,
    key_id: str,
    timestamp: int,
    nonce: str,
    method: str,
    route: str,
    query: Mapping[str, str],
    body_sha256: str,
) -> str:
    return "\n".join(
        [
            ALGORITHM,
            key_id,
            str(timestamp),
            nonce,
            method.upper(),
            route,
            canonical_query(query),
            body_sha256,
        ]
    )


def sign(
    method: str,
    route: str,
    query: Mapping[str, str],
    body_sha256: str,
    key_id: str,
    secret: bytes,
    *,
    now: int,
    nonce: str,
) -> dict[str, str]:
    """Headers that authenticate one request to georg-bridge."""
    message = canonical_string(
        key_id=key_id,
        timestamp=now,
        nonce=nonce,
        method=method,
        route=route,
        query=query,
        body_sha256=body_sha256,
    )
    signature = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    return {
        HEADER_KEY_ID: key_id,
        HEADER_TIMESTAMP: str(now),
        HEADER_NONCE: nonce,
        HEADER_CONTENT_SHA256: body_sha256,
        HEADER_SIGNATURE: signature,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


class FileBody:
    """Request body read from a file; iterable more than once so retries can resend it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __iter__(self) -> Iterator[bytes]:
        with self.path.open("rb") as fh:
            while chunk := fh.read(CHUNK_BYTES):
                yield chunk


class GeorgSigner(httpx.Auth):
    """Signs every request, including each retry, with a fresh timestamp and nonce.

    Requests whose body is streamed must carry a precomputed `X-Georg-Content-SHA256`.
    """

    def __init__(
        self,
        key_id: str,
        secret: bytes,
        *,
        rest_path: str,
        clock: Callable[[], float] = time.time,
        nonce: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        if not KEY_ID_PATTERN.match(key_id):
            raise ValueError("key_id must match [A-Za-z0-9._-]{1,64}")
        self.key_id = key_id
        self._secret = secret
        self._rest_path = "/" + rest_path.strip("/")
        self._clock = clock
        self._nonce = nonce

    def __repr__(self) -> str:
        return f"GeorgSigner(key_id={self.key_id!r})"

    def route(self, url: httpx.URL) -> str:
        path = unquote(url.path)
        if not path.startswith(self._rest_path + "/"):
            raise ValueError(f"{path!r} is not under the REST prefix {self._rest_path!r}")
        return path.removeprefix(self._rest_path)

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        body_sha256 = request.headers.get(HEADER_CONTENT_SHA256)
        if body_sha256 is None:
            body_sha256 = hashlib.sha256(request.content).hexdigest()
        query = dict(request.url.params.multi_items())
        if len(query) != len(request.url.params.multi_items()):
            raise ValueError("repeated query parameters cannot be signed")
        for name in query:
            if not QUERY_KEY_PATTERN.match(name):
                raise ValueError(f"query parameter name {name!r} cannot be signed")
        headers = sign(
            request.method,
            self.route(request.url),
            query,
            body_sha256,
            self.key_id,
            self._secret,
            now=int(self._clock()),
            nonce=self._nonce(),
        )
        request.headers.update(headers)
        yield request
