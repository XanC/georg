# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from georg.core.errors import ConfigError, ModuleError
from georg.gdrive import SCOPE_READONLY, GoogleTokens

TOKEN_URI = "https://oauth2.example.test/token"


def write_key(tmp_path: Path) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "sa.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "georg-test",
                "private_key_id": "throwaway",
                "private_key": pem,
                "client_email": "georg@georg-test.iam.gserviceaccount.com",
                "client_id": "1",
                "token_uri": TOKEN_URI,
            }
        )
    )
    return path


def test_service_account_tokens_sign_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("google.oauth2.service_account")
    monkeypatch.setenv("GEORG_TEST_SA", str(write_key(tmp_path)))
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URI
        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={"access_token": f"tok{len(seen)}", "expires_in": 3600, "token_type": "Bearer"},
        )

    tokens = GoogleTokens(
        "env:GEORG_TEST_SA",
        [SCOPE_READONLY],
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert tokens.token() == "tok1"
    assert tokens.token() == "tok1"
    assert tokens.token(force_refresh=True) == "tok2"
    assert seen[0]["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assert seen[0]["assertion"][0].count(".") == 2


def test_service_account_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEORG_TEST_MISSING", raising=False)
    with pytest.raises(ConfigError):
        GoogleTokens("env:GEORG_TEST_MISSING", [SCOPE_READONLY])
    pytest.importorskip("google.oauth2.service_account")
    tokens = GoogleTokens(str(tmp_path / "absent.json"), [SCOPE_READONLY])
    with pytest.raises(ModuleError) as exc:
        tokens.token()
    assert not exc.value.retryable

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad"})

    tokens = GoogleTokens(
        str(write_key(tmp_path)),
        [SCOPE_READONLY],
        http_client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(ModuleError, match="token refresh failed") as exc:
        tokens.token()
    assert not exc.value.retryable


def write_user(tmp_path: Path) -> Path:
    path = tmp_path / "user.json"
    path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": "cid.apps.googleusercontent.com",
                "client_secret": "csecret",
                "refresh_token": "rtok",
                "token_uri": TOKEN_URI,
            }
        )
    )
    return path


def test_authorized_user_tokens_refresh(tmp_path: Path) -> None:
    pytest.importorskip("google.oauth2.credentials")
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/token")
        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": f"u{len(seen)}", "expires_in": 3600})

    tokens = GoogleTokens(
        str(write_user(tmp_path)),
        [SCOPE_READONLY],
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert tokens.token() == "u1"
    assert tokens.token() == "u1"
    assert tokens.kind == "authorized_user"
    assert seen[0]["grant_type"] == ["refresh_token"]
    assert seen[0]["refresh_token"] == ["rtok"]
    assert "scope" not in seen[0]


def test_authorized_user_revoked_hint(tmp_path: Path) -> None:
    pytest.importorskip("google.oauth2.credentials")

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad"})

    tokens = GoogleTokens(
        str(write_user(tmp_path)),
        [SCOPE_READONLY],
        http_client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(ModuleError, match="georg drive-login") as exc:
        tokens.token()
    assert not exc.value.retryable


def test_credentials_file_kinds(tmp_path: Path) -> None:
    pytest.importorskip("google.oauth2.credentials")
    with pytest.raises(ModuleError, match="needs a service account"):
        GoogleTokens(str(write_user(tmp_path)), [SCOPE_READONLY], subject="a@b.test").token()
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"type": "external_account"}))
    with pytest.raises(ModuleError, match="expected"):
        GoogleTokens(str(other), [SCOPE_READONLY]).token()
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(ModuleError, match="cannot read"):
        GoogleTokens(str(broken), [SCOPE_READONLY]).token()
