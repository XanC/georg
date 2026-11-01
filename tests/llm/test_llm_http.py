# SPDX-License-Identifier: AGPL-3.0-only
from collections.abc import Callable

import httpx
import pytest

from georg.core.errors import ModuleError
from georg.llm import ChatClient, is_retryable_status, post_json

URL = "https://api.example.test/v1/thing"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def scripted(*responses: httpx.Response | Exception) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return client(handler), seen


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (401, False), (403, False), (404, False), (413, False), (422, False),
     (408, True), (429, True), (500, True), (502, True), (503, True)],
)  # fmt: skip
def test_status_classification(status: int, retryable: bool) -> None:
    assert is_retryable_status(status) is retryable


def test_client_error_fails_at_once_and_is_not_retryable() -> None:
    http, seen = scripted(httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(ModuleError, match="HTTP 401: bad key") as info:
        post_json(http, URL, provider="p", attempts=3, sleep=lambda _: None)
    assert info.value.retryable is False
    assert len(seen) == 1


def test_transient_errors_are_retried_then_succeed() -> None:
    http, seen = scripted(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503, text="busy"),
        httpx.Response(200, json={"ok": True}),
    )
    delays: list[float] = []
    assert post_json(http, URL, provider="p", attempts=3, backoff_s=1, sleep=delays.append) == {
        "ok": True
    }
    assert len(seen) == 3
    assert delays == [7.0, 2.0]


def test_exhausted_transient_errors_are_retryable() -> None:
    http, seen = scripted(httpx.Response(500), httpx.Response(502))
    with pytest.raises(ModuleError, match="HTTP 502") as info:
        post_json(http, URL, provider="p", attempts=2, sleep=lambda _: None)
    assert info.value.retryable is True
    assert len(seen) == 2


def test_transport_error_is_retryable() -> None:
    http, _ = scripted(httpx.ConnectError("refused"))
    with pytest.raises(ModuleError, match="ConnectError") as info:
        post_json(http, URL, provider="p", attempts=1)
    assert info.value.retryable is True


def test_non_json_success_is_not_retryable() -> None:
    http, _ = scripted(httpx.Response(200, text="<html>"))
    with pytest.raises(ModuleError, match="not JSON") as info:
        post_json(http, URL, provider="p")
    assert info.value.retryable is False


def test_chat_client_request_and_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        message = {"role": "assistant", "content": "Hi."}
        choice = {"message": message, "finish_reason": "stop"}
        return httpx.Response(200, json={"model": "gpt-4o-mini-2024-07-18", "choices": [choice]})

    chat = ChatClient(base_url="https://llm.test/v1/", api_key="k-123", http_client=client(handler))
    result = chat.complete([{"role": "user", "content": "hi"}], model="m", temperature=0)
    assert result.content == "Hi." and result.finish_reason == "stop"
    assert result.model == "gpt-4o-mini-2024-07-18"
    req = seen[0]
    assert str(req.url) == "https://llm.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k-123"
    assert (
        req.read() == b'{"model":"m","messages":[{"role":"user","content":"hi"}],"temperature":0}'
    )


def test_chat_client_rejects_malformed_response() -> None:
    chat = ChatClient(
        base_url="https://llm.test/v1",
        api_key="k",
        http_client=client(lambda _: httpx.Response(200, json={"choices": []})),
    )
    with pytest.raises(ModuleError, match="unexpected chat response"):
        chat.complete([{"role": "user", "content": "x"}], model="m")
