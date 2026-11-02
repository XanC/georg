# SPDX-License-Identifier: AGPL-3.0-only
"""OAuth access tokens for Google Drive.

Credentials come from a JSON file of one of two kinds: a service account key, or an
"authorized user" file holding the refresh token of a Google account that signed in once
with `georg drive-login` (`georg.gdrive.login`). `google-auth` parses the file and
refreshes tokens; its token request is sent with `httpx` through a small adapter, so no
`requests` dependency is needed.
"""

import importlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from georg.core.config import resolve_secret
from georg.core.errors import ModuleError

SCOPE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"

_EXTRA_HINT = "install the 'gdrive' extra (uv sync --extra gdrive)"


class TokenSource(Protocol):
    def token(self, *, force_refresh: bool = False) -> str:
        """Return a valid bearer token, refreshing it when expired or when asked."""
        ...


def _import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ModuleError(f"google-auth is not installed; {_EXTRA_HINT}") from exc


class _AuthResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.headers = dict(response.headers)
        self.data = response.content


class _AuthRequest:
    """A `google.auth.transport.Request` implemented with httpx."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _AuthResponse:
        try:
            response = self._client.request(
                method,
                url,
                content=body,
                headers=dict(headers or {}),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TransportError as exc:
            exceptions = _import("google.auth.exceptions")
            raise exceptions.TransportError(f"token request failed: {type(exc).__name__}") from exc
        return _AuthResponse(response)


SERVICE_ACCOUNT = "service_account"
AUTHORIZED_USER = "authorized_user"


class GoogleTokens:
    """`TokenSource` backed by a credentials JSON file.

    `credentials_file` is a path or an "env:NAME" reference to one, read on the first
    `token()` call. A service account key is used with `scopes`; `subject` impersonates
    a Workspace user through domain-wide delegation. An authorized-user file keeps the
    scopes granted when the account signed in, and cannot take a `subject`.
    """

    def __init__(
        self,
        credentials_file: str,
        scopes: Sequence[str],
        *,
        subject: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.path = Path(resolve_secret(credentials_file)).expanduser()
        self._scopes = list(scopes)
        self._subject = subject
        self._client = http_client or httpx.Client(timeout=30.0)
        self._credentials: Any = None
        self.kind: str | None = None

    def _read(self) -> dict[str, Any]:
        try:
            info = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise ModuleError(
                f"cannot read Google credentials {self.path}: {type(exc).__name__}"
            ) from exc
        if not isinstance(info, dict):
            raise ModuleError(f"Google credentials {self.path} is not a JSON object")
        return info

    def _load(self) -> Any:
        info = self._read()
        kind = info.get("type")
        try:
            if kind == SERVICE_ACCOUNT:
                service_account = _import("google.oauth2.service_account")
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=self._scopes
                )
                creds = creds.with_subject(self._subject) if self._subject else creds
            elif kind == AUTHORIZED_USER:
                if self._subject:
                    raise ModuleError(
                        "`subject` needs a service account; "
                        f"{self.path} is a signed-in Google account"
                    )
                user = _import("google.oauth2.credentials")
                creds = user.Credentials.from_authorized_user_info(info)
            else:
                raise ModuleError(
                    f"Google credentials {self.path} have type {kind!r}; expected "
                    f"{SERVICE_ACCOUNT!r} or {AUTHORIZED_USER!r}"
                )
        except (ValueError, KeyError) as exc:
            raise ModuleError(
                f"cannot read Google credentials {self.path}: {type(exc).__name__}"
            ) from exc
        self.kind = kind
        return creds

    def token(self, *, force_refresh: bool = False) -> str:
        if self._credentials is None:
            self._credentials = self._load()
        creds = self._credentials
        if force_refresh or not creds.valid:
            exceptions = _import("google.auth.exceptions")
            try:
                creds.refresh(_AuthRequest(self._client))
            except exceptions.RefreshError as exc:
                retryable = bool(getattr(exc, "retryable", False))
                message = f"Google token refresh failed: {exc}"
                if self.kind == AUTHORIZED_USER and not retryable:
                    message += "; the sign-in may have been revoked: run `georg drive-login`"
                raise ModuleError(message, retryable=retryable) from exc
            except exceptions.TransportError as exc:
                raise ModuleError(f"Google token refresh failed: {exc}", retryable=True) from exc
        token = creds.token
        if not isinstance(token, str) or not token:
            raise ModuleError("Google token refresh returned no access token", retryable=True)
        return token
