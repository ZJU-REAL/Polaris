"""OpenAI 兼容接口 Provider（DeepSeek / vLLM / OpenRouter 等），基于 httpx。

骨架实现：complete/stream 接口完整；重试、超时策略、tool-use 等留 TODO。
"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.llm.base import CompletionResult, LLMProvider, Message


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _payload(
        self,
        messages: Sequence[Message],
        model: str,
        temperature: float,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        # TODO(M2): 重试/限速/错误分类
        resp = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(messages, model, temperature, max_tokens, stream=False),
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        return CompletionResult(
            content=choice["message"]["content"] or "",
            model=data.get("model", model),
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
        )

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        # TODO(M2): usage 统计、错误恢复
        async with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(messages, model, temperature, max_tokens, stream=True),
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if chunk == "[DONE]":
                    break
                delta = json.loads(chunk)["choices"][0].get("delta", {})
                if content := delta.get("content"):
                    yield content

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        resp = await self._client.post(
            f"{self._base_url}/embeddings",
            headers=self._headers(),
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # 按 index 还原顺序（OpenAI 兼容端点保证有 index 字段）
        data.sort(key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in data]

    async def aclose(self) -> None:
        await self._client.aclose()
