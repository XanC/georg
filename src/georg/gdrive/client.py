# SPDX-License-Identifier: AGPL-3.0-only
"""Drive v3 REST calls with bounded retries and `ModuleError` classification.

Transport errors, 408/429/5xx and Drive's rate-limit 403s are transient and become
`ModuleError(retryable=True)` once the attempts are used up; a 401 refreshes the token
once. Other 4xx responses fail immediately. Authorization headers never appear in
error messages.
"""

import hashlib
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from georg.core.errors import ModuleError
from georg.gdrive.auth import TokenSource
from georg.llm.http import MAX_DELAY_S, is_retryable_status

API_URL = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
CHUNK_ALIGN = 256 * 1024
_DOWNLOAD_CHUNK = 1024 * 1024
_DETAIL_CHARS = 300


def quote_query(value: str) -> str:
    """Escape a value for use inside single quotes in a Drive `q` expression."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def parse_time(value: object) -> datetime | None:
    """Parse Drive's RFC 3339 timestamps (e.g. "2026-11-01T18:23:45.123Z")."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _reasons(response: httpx.Response) -> tuple[str, str]:
    try:
        data = response.json()
    except ValueError:
        return "", response.text[:_DETAIL_CHARS]
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "", response.text[:_DETAIL_CHARS]
    reasons = [
        e.get("reason", "") for e in error.get("errors", []) or [] if isinstance(e, dict)
    ] or [str(error.get("status", ""))]
    message = error.get("message")
    return ",".join(r for r in reasons if r), str(message or "")[:_DETAIL_CHARS]


def _error(response: httpx.Response, action: str) -> ModuleError:
    status = response.status_code
    reason, detail = _reasons(response)
    retryable = is_retryable_status(status) or (
        status == 403 and bool(set(reason.split(",")) & RATE_LIMIT_REASONS)
    )
    message = f"google drive {action}: HTTP {status}"
    if reason:
        message += f" ({reason})"
    if detail:
        message += f": {detail}"
    if "storageQuotaExceeded" in reason:
        message += (
            "; service accounts have no storage of their own: upload into a shared drive, "
            "impersonate a user with `subject`, or sign in as a Google account with "
            "`georg drive-login`"
        )
    return ModuleError(message, retryable=retryable)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _next_offset(range_header: str | None) -> int:
    """Byte offset to resume from, given a 308 response's "Range: bytes=0-N" header."""
    if not range_header or "-" not in range_header:
        return 0
    return int(range_header.rsplit("-", 1)[1]) + 1


class DriveClient:
    def __init__(
        self,
        tokens: TokenSource,
        http_client: httpx.Client,
        *,
        attempts: int = 4,
        backoff_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self._tokens = tokens
        self._client = http_client
        self._attempts = attempts
        self._backoff_s = backoff_s
        self._sleep = sleep

    def _auth(self, force_refresh: bool = False) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.token(force_refresh=force_refresh)}"}

    def _wait(self, attempt: int, response: httpx.Response | None = None) -> None:
        delay = self._backoff_s * 2 ** (attempt - 1)
        if response is not None:
            delay = _retry_after(response) or delay
        self._sleep(min(delay, MAX_DELAY_S))

    def request(
        self,
        method: str,
        url: str,
        *,
        action: str,
        accept: frozenset[int] = frozenset(),
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send an authorized request; return successful (or `accept`ed) responses."""
        refreshed = False
        attempt = 1
        while True:
            failed: httpx.Response | None = None
            try:
                response = self._client.request(
                    method, url, headers={**(headers or {}), **self._auth(refreshed)}, **kwargs
                )
            except httpx.TransportError as exc:
                error = ModuleError(
                    f"google drive {action}: request failed: {type(exc).__name__}",
                    retryable=True,
                )
            else:
                if response.is_success or response.status_code in accept:
                    return response
                if response.status_code == 401 and not refreshed:
                    refreshed = True
                    continue
                failed = response
                error = _error(response, action)
                if not error.retryable:
                    raise error
            if attempt >= self._attempts:
                raise error
            self._wait(attempt, failed)
            attempt += 1

    def _json(self, response: httpx.Response, action: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ModuleError(f"google drive {action}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ModuleError(f"google drive {action}: response is not a JSON object")
        return data

    def list_files(
        self,
        query: str,
        fields: str,
        *,
        all_drives: bool = True,
        corpora: str | None = None,
        order_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every file matching `query`, following `nextPageToken`."""
        params: dict[str, str | int] = {
            "q": query,
            "fields": f"nextPageToken, files({fields})",
            "pageSize": 1000,
        }
        if all_drives:
            params.update(supportsAllDrives="true", includeItemsFromAllDrives="true")
        if corpora:
            params["corpora"] = corpora
        if order_by:
            params["orderBy"] = order_by
        while True:
            response = self.request("GET", f"{API_URL}/files", action="list", params=params)
            data = self._json(response, "list")
            for item in data.get("files", []):
                if isinstance(item, dict):
                    yield item
            token = data.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    def get_file(self, file_id: str, fields: str) -> dict[str, Any] | None:
        """Return the file's metadata, or None if it does not exist (or is not shared)."""
        response = self.request(
            "GET",
            f"{API_URL}/files/{file_id}",
            action=f"get {file_id}",
            accept=frozenset({404}),
            params={"fields": fields, "supportsAllDrives": "true"},
        )
        if response.status_code == 404:
            return None
        return self._json(response, f"get {file_id}")

    def create_folder(self, name: str, parent: str) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"{API_URL}/files",
            action=f"create folder {name!r}",
            params={"fields": "id, name", "supportsAllDrives": "true"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
        )
        return self._json(response, f"create folder {name!r}")

    def download(self, file_id: str, dest: Path) -> tuple[int, str]:
        """Stream the file's content into `dest` (overwritten); return (size, md5 hex)."""
        url = f"{API_URL}/files/{file_id}"
        params = {"alt": "media", "supportsAllDrives": "true"}
        action = f"download {file_id}"
        refreshed = False
        attempt = 1
        while True:
            response: httpx.Response | None = None
            try:
                with self._client.stream(
                    "GET", url, params=params, headers=self._auth(refreshed)
                ) as response:
                    if response.status_code == 401 and not refreshed:
                        refreshed = True
                        continue
                    if not response.is_success:
                        response.read()
                        error = _error(response, action)
                        if not error.retryable:
                            raise error
                    else:
                        md5 = hashlib.md5(usedforsecurity=False)
                        size = 0
                        with dest.open("wb") as out:
                            for chunk in response.iter_bytes(_DOWNLOAD_CHUNK):
                                out.write(chunk)
                                md5.update(chunk)
                                size += len(chunk)
                        return size, md5.hexdigest()
            except httpx.TransportError as exc:
                error = ModuleError(
                    f"google drive {action}: transfer failed: {type(exc).__name__}",
                    retryable=True,
                )
            except OSError as exc:
                raise ModuleError(f"google drive {action}: cannot write {dest}: {exc}") from exc
            if attempt >= self._attempts:
                raise error
            self._wait(attempt, response)
            attempt += 1

    def upload(
        self,
        path: Path,
        metadata: Mapping[str, Any],
        *,
        mime: str,
        fields: str,
        file_id: str | None = None,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Resumable upload of `path`: a new file, or new content for `file_id`."""
        if chunk_size <= 0 or chunk_size % CHUNK_ALIGN:
            raise ValueError("chunk_size must be a positive multiple of 256 KiB")
        total = path.stat().st_size
        action = f"upload {path.name}"
        target = f"{UPLOAD_URL}/files" + (f"/{file_id}" if file_id else "")
        start = self.request(
            "PATCH" if file_id else "POST",
            target,
            action=action,
            params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": fields},
            headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(total)},
            json=dict(metadata),
        )
        session = start.headers.get("location")
        if not session:
            raise ModuleError(f"google drive {action}: no resumable session URL", retryable=True)
        offset = 0
        query_status = False
        failures = 0
        with path.open("rb") as f:
            while True:
                if query_status:
                    chunk = b""
                    content_range = f"bytes */{total}"
                else:
                    f.seek(offset)
                    chunk = f.read(chunk_size)
                    end = offset + len(chunk) - 1
                    content_range = f"bytes {offset}-{end}/{total}" if chunk else f"bytes */{total}"
                response: httpx.Response | None = None
                try:
                    response = self._client.put(
                        session, content=chunk, headers={"Content-Range": content_range}
                    )
                except httpx.TransportError as exc:
                    error = ModuleError(
                        f"google drive {action}: transfer failed: {type(exc).__name__}",
                        retryable=True,
                    )
                else:
                    if response.status_code in (200, 201):
                        return self._json(response, action)
                    if response.status_code == 308:
                        received = _next_offset(response.headers.get("range"))
                        if received > offset:
                            failures = 0
                        offset = received
                        query_status = False
                        continue
                    error = _error(response, action)
                    if response.status_code in (404, 410):
                        error = ModuleError(
                            f"google drive {action}: upload session expired", retryable=True
                        )
                        raise error
                    if not error.retryable:
                        raise error
                failures += 1
                if failures >= self._attempts:
                    raise error
                self._wait(failures, response)
                query_status = True
