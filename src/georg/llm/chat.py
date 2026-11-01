# SPDX-License-Identifier: AGPL-3.0-only
"""Client for OpenAI-compatible `/chat/completions` endpoints."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

import httpx

from georg.core.errors import ModuleError
from georg.llm.http import post_json


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ChatResult:
    content: str
    finish_reason: str | None
    model: str | None


class ChatClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        provider: str = "chat",
        timeout_s: float = 180.0,
        attempts: int = 3,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._provider = provider
        self._attempts = attempts
        self._client = http_client or httpx.Client(timeout=timeout_s)
        self._sleep = sleep

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float | None = None,
    ) -> ChatResult:
        body: dict[str, object] = {"model": model, "messages": list(messages)}
        if temperature is not None:
            body["temperature"] = temperature
        data = post_json(
            self._client,
            self._url,
            provider=self._provider,
            attempts=self._attempts,
            sleep=self._sleep,
            json=body,
            headers=self._headers,
        )
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModuleError(f"{self._provider}: unexpected chat response shape") from exc
        if not isinstance(content, str):
            raise ModuleError(f"{self._provider}: chat response has no text content")
        model_name = data.get("model")
        return ChatResult(
            content=content,
            finish_reason=choice.get("finish_reason"),
            model=model_name if isinstance(model_name, str) else None,
        )
