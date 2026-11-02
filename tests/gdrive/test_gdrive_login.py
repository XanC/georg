# SPDX-License-Identifier: AGPL-3.0-only
import json
import stat
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from georg.gdrive import SCOPE_DRIVE
from georg.gdrive.login import (
    ABOUT_URL,
    LoginError,
    OAuthClient,
    authorization_url,
    load_client,
    login,
    parse_redirect,
    pkce_pair,
)

CLIENT = OAuthClient(
    client_id="cid.apps.googleusercontent.com",
    client_secret="csecret",
    token_uri="https://oauth2.example.test/token",
)


def google(seen: list[dict[str, list[str]]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(CLIENT.token_uri):
            seen.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt"})
        assert str(request.url).startswith(ABOUT_URL)
        assert request.headers["authorization"] == "Bearer at"
        return httpx.Response(200, json={"user": {"emailAddress": "church@example.test"}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_load_client(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}))
    assert load_client(path) == OAuthClient("a", "b")
    path.write_text(json.dumps({"web": {"client_id": "a", "client_secret": "b"}}))
    with pytest.raises(LoginError, match="Desktop app"):
        load_client(path)
    path.write_text(json.dumps({"installed": {"client_id": "a"}}))
    with pytest.raises(LoginError, match="client_secret"):
        load_client(path)


def test_authorization_url_requests_offline_pkce() -> None:
    verifier, challenge = pkce_pair()
    assert verifier != challenge
    params = query(authorization_url(CLIENT, "http://127.0.0.1:5/", SCOPE_DRIVE, "st", challenge))
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] == challenge
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["scope"] == SCOPE_DRIVE


def test_parse_redirect() -> None:
    assert parse_redirect("http://127.0.0.1:5/?state=s&code=c", "s") == "c"
    with pytest.raises(LoginError, match="state"):
        parse_redirect("http://127.0.0.1:5/?state=x&code=c", "s")
    with pytest.raises(LoginError, match="access_denied"):
        parse_redirect("http://127.0.0.1:5/?error=access_denied&state=s", "s")
    with pytest.raises(LoginError, match="no authorization code"):
        parse_redirect("http://127.0.0.1:5/?state=s", "s")


def test_login_pasted_redirect(tmp_path: Path) -> None:
    seen: list[dict[str, list[str]]] = []
    lines: list[str] = []

    def ask(prompt: str) -> str:
        url = query(next(line for line in lines if line.startswith("https://")))
        return f"{url['redirect_uri']}?state={url['state']}&code=thecode&scope=x"

    out = tmp_path / "creds.json"
    email = login(CLIENT, out, paste=True, http=google(seen), say=lines.append, ask=ask)
    assert email == "church@example.test"
    assert seen[0]["code"] == ["thecode"]
    assert seen[0]["grant_type"] == ["authorization_code"]
    assert len(seen[0]["code_verifier"][0]) >= 43
    assert json.loads(out.read_text()) == {
        "type": "authorized_user",
        "client_id": CLIENT.client_id,
        "client_secret": CLIENT.client_secret,
        "refresh_token": "rt",
        "token_uri": CLIENT.token_uri,
    }
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_login_loopback_server(tmp_path: Path) -> None:
    seen: list[dict[str, list[str]]] = []

    def say(line: str) -> None:
        if not line.startswith("https://"):
            return
        url = query(line)
        target = f"{url['redirect_uri']}?state={url['state']}&code=loop"

        def browse() -> None:
            assert httpx.get(url["redirect_uri"] + "favicon.ico", timeout=5).status_code == 404
            assert httpx.get(target, timeout=5).status_code == 200

        threading.Thread(target=browse, daemon=True).start()

    out = tmp_path / "creds.json"
    login(CLIENT, out, open_browser=False, timeout_s=10, http=google(seen), say=say)
    assert seen[0]["code"] == ["loop"]
    assert seen[0]["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert out.exists()


def test_login_without_refresh_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at"})

    lines: list[str] = []

    def ask(prompt: str) -> str:
        url = query(next(line for line in lines if line.startswith("https://")))
        return f"{url['redirect_uri']}?state={url['state']}&code=c"

    with pytest.raises(LoginError, match="no refresh token"):
        login(
            CLIENT,
            tmp_path / "c.json",
            paste=True,
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            say=lines.append,
            ask=ask,
        )
    assert not (tmp_path / "c.json").exists()
