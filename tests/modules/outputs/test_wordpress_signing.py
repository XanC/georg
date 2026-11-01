# SPDX-License-Identifier: AGPL-3.0-only
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from georg.modules.outputs.wordpress_signing import (
    EMPTY_SHA256,
    FileBody,
    GeorgSigner,
    canonical_query,
    canonical_string,
    decode_secret,
    file_sha256,
    sign,
)

VECTORS = Path(__file__).parents[3] / "wordpress" / "georg-bridge" / "tests" / "vectors.json"


def _vectors() -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads(VECTORS.read_text(encoding="utf-8"))
    return list(data["vectors"])


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: str(v["name"]))
def test_vectors(vector: dict[str, Any]) -> None:
    body = base64.b64decode(vector["body_base64"])
    assert hashlib.sha256(body).hexdigest() == vector["content_sha256"]
    fields = {
        "key_id": vector["key_id"],
        "timestamp": vector["timestamp"],
        "nonce": vector["nonce"],
        "method": vector["method"],
        "route": vector["route"],
        "query": vector["query"],
        "body_sha256": vector["content_sha256"],
    }
    assert canonical_string(**fields) == vector["canonical"]
    headers = sign(
        vector["method"],
        vector["route"],
        vector["query"],
        vector["content_sha256"],
        vector["key_id"],
        decode_secret(vector["secret"]),
        now=vector["timestamp"],
        nonce=vector["nonce"],
    )
    assert headers == {
        "X-Georg-Key-Id": vector["key_id"],
        "X-Georg-Timestamp": str(vector["timestamp"]),
        "X-Georg-Nonce": vector["nonce"],
        "X-Georg-Content-SHA256": vector["content_sha256"],
        "X-Georg-Signature": vector["signature"],
    }


def test_canonical_query_matches_rawurlencode() -> None:
    assert canonical_query({}) == ""
    assert canonical_query({"rest_route": "/georg/v1/audio"}) == ""
    assert canonical_query({"b": " +~/", "a": "1", "Z": "é"}) == "Z=%C3%A9&a=1&b=%20%2B~%2F"


def test_decode_secret() -> None:
    raw = bytes(range(32))
    assert decode_secret(base64.urlsafe_b64encode(raw).decode()) == raw
    assert decode_secret(base64.urlsafe_b64encode(raw).rstrip(b"=").decode()) == raw
    for bad in ("", "a" * 42, "not base64*", base64.b64encode(b"\xff" * 33).decode()):
        with pytest.raises(ValueError):
            decode_secret(bad)


def test_signer_signs_route_query_and_body() -> None:
    signer = GeorgSigner(
        "k1", b"s" * 32, rest_path="/blog/wp-json", clock=lambda: 1000.9, nonce=lambda: "n" * 32
    )
    request = httpx.Request(
        "PUT",
        "https://church.example/blog/wp-json/georg/v1/sermons/georg%3Aa%3Asermon-1%3Aw",
        params={"x": "1 2"},
        content=b"{}",
    )
    signed = next(signer.auth_flow(request))
    expected = sign(
        "PUT",
        "/georg/v1/sermons/georg:a:sermon-1:w",
        {"x": "1 2"},
        hashlib.sha256(b"{}").hexdigest(),
        "k1",
        b"s" * 32,
        now=1000,
        nonce="n" * 32,
    )
    for name, value in expected.items():
        assert signed.headers[name] == value
    assert "s" * 32 not in repr(signer)


def test_signer_uses_precomputed_hash_for_streams(tmp_path: Path) -> None:
    path = tmp_path / "a.mp3"
    path.write_bytes(b"x" * (3 << 20))
    body = FileBody(path)
    assert b"".join(body) == b"".join(body) == path.read_bytes()
    digest = file_sha256(path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    signer = GeorgSigner("k1", b"s" * 32, rest_path="/wp-json")
    request = httpx.Request(
        "POST",
        "https://x/wp-json/georg/v1/audio",
        content=body,
        headers={"X-Georg-Content-SHA256": digest},
    )
    assert next(signer.auth_flow(request)).headers["X-Georg-Content-SHA256"] == digest
    assert hashlib.sha256(b"").hexdigest() == EMPTY_SHA256


def test_signer_rejects_unsignable_requests() -> None:
    signer = GeorgSigner("k1", b"s" * 32, rest_path="/wp-json")
    for url in ("https://x/other/georg/v1/audio", "https://x/wp-json/georg/v1/audio?a.b=1"):
        with pytest.raises(ValueError):
            next(signer.auth_flow(httpx.Request("GET", url)))
    with pytest.raises(ValueError):
        GeorgSigner("bad id", b"s" * 32, rest_path="/wp-json")
