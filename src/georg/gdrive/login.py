# SPDX-License-Identifier: AGPL-3.0-only
"""One-time Google sign-in that saves a refresh token for the Drive modules.

This is OAuth's flow for installed apps: Georg opens Google's consent page, Google
redirects the browser to a loopback address with a one-time code, and Georg exchanges
the code (with PKCE) for a refresh token. The result is an "authorized user" JSON file
that `georg.gdrive.auth.GoogleTokens` reads like a service account key.

The OAuth client is a "Desktop app" client from the church's own Google Cloud project;
its consent screen must be published ("In production"), because Google expires refresh
tokens issued to apps in testing after seven days.
"""

import base64
import hashlib
import html
import json
import os
import secrets
import socket
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from georg.core.errors import GeorgError
from georg.gdrive.auth import AUTHORIZED_USER, SCOPE_DRIVE

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about"


class LoginError(GeorgError):
    """The sign-in could not be completed."""


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str
    auth_uri: str = AUTH_URI
    token_uri: str = TOKEN_URI


def load_client(path: Path) -> OAuthClient:
    """Read the JSON downloaded for a Google Cloud "Desktop app" OAuth client."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LoginError(f"cannot read OAuth client file {path}: {type(exc).__name__}") from exc
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        raise LoginError(f"{path} is not a Desktop app OAuth client (no 'installed' section)")
    try:
        return OAuthClient(
            client_id=str(installed["client_id"]),
            client_secret=str(installed["client_secret"]),
            auth_uri=str(installed.get("auth_uri", AUTH_URI)),
            token_uri=str(installed.get("token_uri", TOKEN_URI)),
        )
    except KeyError as exc:
        raise LoginError(f"{path} lacks {exc.args[0]!r}") from None


def pkce_pair() -> tuple[str, str]:
    """A PKCE code verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(
    client: OAuthClient, redirect_uri: str, scope: str, state: str, challenge: str
) -> str:
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{client.auth_uri}?{urlencode(query)}"


def parse_redirect(url: str, state: str) -> str:
    """Return the authorization code from the URL Google redirected the browser to."""
    params = parse_qs(urlsplit(url.strip()).query)
    if "error" in params:
        raise LoginError(f"Google refused the sign-in: {params['error'][0]}")
    if params.get("state", [""])[0] != state:
        raise LoginError("the redirect does not belong to this sign-in (state mismatch)")
    code = params.get("code", [""])[0]
    if not code:
        raise LoginError("the redirect carries no authorization code")
    return code


def exchange_code(
    client: OAuthClient, code: str, verifier: str, redirect_uri: str, http: httpx.Client
) -> dict[str, Any]:
    response = http.post(
        client.token_uri,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        },
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict):
        reason = body.get("error", "") if isinstance(body, dict) else ""
        raise LoginError(f"token exchange failed: HTTP {response.status_code} {reason}".strip())
    if not body.get("refresh_token"):
        raise LoginError("Google returned no refresh token; remove Georg's access and retry")
    return body


def write_authorized_user(path: Path, client: OAuthClient, refresh_token: str) -> None:
    """Write the credentials file, readable only by its owner."""
    info = {
        "type": AUTHORIZED_USER,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "refresh_token": refresh_token,
        "token_uri": client.token_uri,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f, indent=2)
        f.write("\n")


def signed_in_as(access_token: str, http: httpx.Client) -> str | None:
    """The Drive account's email address, if Drive reports it."""
    response = http.get(
        ABOUT_URL,
        params={"fields": "user(emailAddress)"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if response.status_code != 200:
        return None
    email = response.json().get("user", {}).get("emailAddress")
    return email if isinstance(email, str) else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class _Redirect(HTTPServer):
    url: str | None = None


class _Handler(BaseHTTPRequestHandler):
    server: _Redirect

    def do_GET(self) -> None:
        if "code=" not in self.path and "error=" not in self.path:
            self.send_error(404)
            return
        self.server.url = self.path
        page = html.escape("Georg received the sign-in. You can close this tab.")
        body = f"<!doctype html><title>Georg</title><p>{page}</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def wait_for_redirect(server: _Redirect, timeout_s: float) -> str:
    """Serve loopback requests until Google's redirect arrives."""
    server.timeout = 1.0
    stop = threading.Event()
    timer = threading.Timer(timeout_s, stop.set)
    timer.start()
    try:
        while server.url is None and not stop.is_set():
            server.handle_request()
    finally:
        timer.cancel()
        server.server_close()
    if server.url is None:
        raise LoginError("timed out waiting for the browser to return from Google")
    return server.url


def login(
    client: OAuthClient,
    out: Path,
    *,
    scope: str = SCOPE_DRIVE,
    paste: bool = False,
    open_browser: bool = True,
    timeout_s: float = 300.0,
    http: httpx.Client | None = None,
    say: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
) -> str | None:
    """Run the sign-in, write `out`, and return the signed-in email address."""
    http = http or httpx.Client(timeout=30.0)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    server: _Redirect | None = None
    if paste:
        port = _free_port()
    else:
        server = _Redirect(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"
    url = authorization_url(client, redirect_uri, scope, state, challenge)
    say("Open this address in a browser and sign in with the Google account Georg should use:")
    say(url)
    if server is None:
        say(
            "Google then sends the browser to an address starting with "
            f"{redirect_uri} that will not load. Copy that whole address from the "
            "address bar."
        )
        returned = ask("Address: ")
    else:
        if open_browser:
            webbrowser.open(url)
        returned = wait_for_redirect(server, timeout_s)
    code = parse_redirect(returned, state)
    tokens = exchange_code(client, code, verifier, redirect_uri, http)
    write_authorized_user(out, client, str(tokens["refresh_token"]))
    access = tokens.get("access_token")
    return signed_in_as(access, http) if isinstance(access, str) else None
