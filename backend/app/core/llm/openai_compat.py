"""OpenAI 兼容接口 Provider（DeepSeek / vLLM / OpenRouter 等），基于 httpx。

429/5xx 自动指数退避重试（尊重 Retry-After）；tool-use 留 TODO。
"""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.llm.base import CompletionResult, LLMProvider, Message, RerankResult

logger = logging.getLogger("polaris.llm")

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 3.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return min(60.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return _BACKOFF_BASE_SECONDS * (2**attempt)


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    async def _post_with_retry(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """429/5xx/网络错误重试（指数退避，尊重 Retry-After），其余状态原样返回。"""
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await self._client.post(url, headers=self._headers(), json=payload)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
                delay = _retry_delay(resp, attempt)
                logger.warning(
                    "openai_compat %s，%.0fs 后重试（%d/%d）：%s",
                    resp.status_code,
                    delay,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    url,
                )
                await asyncio.sleep(delay)
                continue
            return resp
        raise RuntimeError(f"openai_compat 请求 {url} 重试 {_MAX_ATTEMPTS} 次后仍失败：{last_exc}")

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
        resp = await self._post_with_retry(
            f"{self._base_url}/chat/completions",
            self._payload(messages, model, temperature, max_tokens, stream=False, images=images),
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
        resp = await self._post_with_retry(
            f"{self._base_url}/embeddings", {"model": model, "input": texts}
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
        resp = await self._post_with_retry(f"{self._base_url}/rerank", payload)
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
