# SPDX-License-Identifier: AGPL-3.0-only
"""OAuth access tokens for a Google service account.

`google-auth` signs the JWT assertion and parses the key file; its token request is
sent with `httpx` through a small adapter, so no `requests` dependency is needed.
"""

import importlib
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


class ServiceAccountTokens:
    """`TokenSource` backed by a service account JSON key file.

    `credentials_file` is a path or an "env:NAME" reference to one. The file is read on
    the first `token()` call. `subject` impersonates a Workspace user through
    domain-wide delegation.
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

    def _load(self) -> Any:
        service_account = _import("google.oauth2.service_account")
        try:
            creds = service_account.Credentials.from_service_account_file(
                str(self.path), scopes=self._scopes
            )
        except (OSError, ValueError, KeyError) as exc:
            raise ModuleError(
                f"cannot read Google service account key {self.path}: {type(exc).__name__}"
            ) from exc
        return creds.with_subject(self._subject) if self._subject else creds

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
                raise ModuleError(
                    f"Google token refresh failed: {exc}", retryable=retryable
                ) from exc
            except exceptions.TransportError as exc:
                raise ModuleError(f"Google token refresh failed: {exc}", retryable=True) from exc
        token = creds.token
        if not isinstance(token, str) or not token:
            raise ModuleError("Google token refresh returned no access token", retryable=True)
        return token
