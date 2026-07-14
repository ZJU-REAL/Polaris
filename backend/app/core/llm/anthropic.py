"""Anthropic Messages API Provider，基于 httpx（不依赖官方 SDK）。

骨架实现：complete/stream 接口完整；tool-use、缓存、重试留 TODO。
"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.llm.base import CompletionResult, LLMProvider, Message

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "anthropic-version": _API_VERSION}

    @staticmethod
    def _payload(
        messages: Sequence[Message],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        # Anthropic 的 system 提示是顶层参数，不在 messages 里
        system_parts = [m.content for m in messages if m.role == "system"]
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages if m.role != "system"
            ],
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        return payload

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        # TODO(M2): 重试/限速/错误分类
        resp = await self._client.post(
            _API_URL,
            headers=self._headers(),
            json=self._payload(messages, model, temperature, max_tokens, stream=False),
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        return CompletionResult(
            content=text,
            model=data.get("model", model),
            finish_reason=data.get("stop_reason"),
            usage=data.get("usage") or {},
        )

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        # TODO(M2): 处理 message_delta 中的 usage / stop_reason
        async with self._client.stream(
            "POST",
            _API_URL,
            headers=self._headers(),
            json=self._payload(messages, model, temperature, max_tokens, stream=True),
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                if event.get("type") == "content_block_delta" and (
                    text := event.get("delta", {}).get("text")
                ):
                    yield text

    async def aclose(self) -> None:
        await self._client.aclose()
