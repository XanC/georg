# SPDX-License-Identifier: AGPL-3.0-only
"""Requests to JSON APIs with bounded retries and `ModuleError` classification.

Transport errors, timeouts, 408/409/425/429 and 5xx responses are transient and raise
`ModuleError(retryable=True)` once the attempts are used up; other 4xx responses fail
immediately and are not retryable. Request headers (which hold API keys) are never
included in error messages.
"""

import time
from collections.abc import Callable
from typing import Any

import httpx

from georg.core.errors import ModuleError

RETRYABLE_STATUS = frozenset({408, 409, 425, 429})
MAX_DELAY_S = 60.0
_DETAIL_CHARS = 300


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS or status >= 500


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:_DETAIL_CHARS]
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        message = data["error"].get("message")
        if isinstance(message, str):
            return message[:_DETAIL_CHARS]
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        code = data.get("code")
        prefix = f"{code}: " if isinstance(code, str) else ""
        return f"{prefix}{data['message']}"[:_DETAIL_CHARS]
    return response.text[:_DETAIL_CHARS]


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    attempts: int = 3,
    backoff_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    none_on: frozenset[int] = frozenset(),
    **kwargs: Any,
) -> Any:
    """Send the request and return the decoded JSON body, retrying transient failures.

    Responses whose status is in `none_on` (e.g. 404 for a lookup) return None.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        delay = backoff_s * 2 ** (attempt - 1)
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            error = ModuleError(f"{provider}: request failed: {type(exc).__name__}", retryable=True)
        else:
            if response.status_code in none_on:
                return None
            if response.is_success:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ModuleError(f"{provider}: response is not JSON") from exc
            status = response.status_code
            retryable = is_retryable_status(status)
            error = ModuleError(
                f"{provider}: HTTP {status}: {_detail(response)}", retryable=retryable
            )
            if not retryable:
                raise error
            delay = _retry_after(response) or delay
        if attempt == attempts:
            raise error
        sleep(min(delay, MAX_DELAY_S))
    raise AssertionError("unreachable")


def post_json(
    client: httpx.Client,
    url: str,
    *,
    provider: str,
    attempts: int = 3,
    backoff_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """POST and return the decoded JSON body, retrying transient failures."""
    return request_json(
        client,
        "POST",
        url,
        provider=provider,
        attempts=attempts,
        backoff_s=backoff_s,
        sleep=sleep,
        **kwargs,
    )
