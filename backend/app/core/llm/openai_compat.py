"""OpenAI 兼容接口 Provider（DeepSeek / vLLM / OpenRouter 等），基于 httpx。

骨架实现：complete/stream 接口完整；重试、超时策略、tool-use 等留 TODO。
"""

import base64
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.llm.base import CompletionResult, LLMProvider, Message, RerankResult


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
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        payload_messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content} for m in messages
        ]
        if images:
            # 多模态：图片以 data-url image_url parts 附在最后一条 user 消息上
            target = next(
                (m for m in reversed(payload_messages) if m["role"] == "user"),
                payload_messages[-1],
            )
            parts: list[dict[str, Any]] = [{"type": "text", "text": target["content"]}]
            for image in images:
                b64 = base64.b64encode(image).decode("ascii")
                parts.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                )
            target["content"] = parts
        payload: dict[str, Any] = {
            "model": model,
            "messages": payload_messages,
            "stream": stream,
        }
        if temperature is not None:  # 新款 Claude 等模型已弃用该参数，None 则不发送
            payload["temperature"] = temperature
        # Anthropic 系模型（经 LiteLLM 等代理）强制要求 max_tokens，缺省给足额度
        payload["max_tokens"] = max_tokens if max_tokens is not None else 8192
        return payload

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
    ) -> CompletionResult:
        # TODO(M2): 重试/限速/错误分类
        resp = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(
                messages, model, temperature, max_tokens, stream=False, images=images
            ),
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise RuntimeError(f"openai_compat {resp.status_code} from {self._base_url}: {body}")
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
        temperature: float | None = None,
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

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        model: str,
        top_n: int | None = None,
    ) -> RerankResult:
        """Cohere 风格 rerank 端点（LiteLLM 代理 /v1/rerank；base_url 已含 /v1）。"""
        payload: dict[str, Any] = {"model": model, "query": query, "documents": documents}
        if top_n is not None:
            payload["top_n"] = top_n
        resp = await self._client.post(
            f"{self._base_url}/rerank",
            headers=self._headers(),
            json=payload,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise RuntimeError(f"openai_compat {resp.status_code} from {self._base_url}: {body}")
        data = resp.json()
        results = sorted(
            ((int(r["index"]), float(r["relevance_score"])) for r in data["results"]),
            key=lambda pair: -pair[1],
        )
        if top_n is not None:
            results = results[:top_n]
        # 计费：Cohere 风格 meta.billed_units.total_tokens；部分代理放在 usage.total_tokens
        billed = (data.get("meta") or {}).get("billed_units") or {}
        total_tokens = billed.get("total_tokens") or (data.get("usage") or {}).get("total_tokens")
        usage = {"total_tokens": int(total_tokens)} if total_tokens else {}
        return RerankResult(results=results, usage=usage)

    async def aclose(self) -> None:
        await self._client.aclose()
