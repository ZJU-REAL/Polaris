"""Anthropic Messages API Provider，基于 httpx（不依赖官方 SDK）。

complete / stream / stream_events 三个接口完整。429 / 529（过载）/ 5xx / 网络错误按
指数退避重试（尊重 Retry-After），与 OpenAI 兼容 provider 同一口径（#821）。
"""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.llm.base import (
    CompletionResult,
    ContentBlock,
    EffortLevel,
    ImageBlock,
    LLMProvider,
    Message,
    StreamDone,
    StreamEvent,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolResultBlock,
    ToolUseArgsDelta,
    ToolUseBlock,
    ToolUseStart,
    ToolUseStop,
    normalize_finish_reason,
    normalize_usage,
)

logger = logging.getLogger("polaris.llm")

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096
# 老模型不认 output_config.effort（该参数只在 4.6+ 上 GA）；命中即去掉重试一次
_EFFORT_REJECT_MARKERS = ("effort", "output_config")
# 值得重试的状态：529 是 Anthropic 的「服务过载」，高峰期很常见；408 是网关超时。
# 以前一律不重试——一次 429 或 529 就让整个环节失败，而 OpenAI 兼容 provider 早就会退避
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_BACKOFF_BASE_SECONDS = 1.0


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """优先听 Retry-After（封顶 60 秒），没有就指数退避。"""
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return min(60.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return _BACKOFF_BASE_SECONDS * (2**attempt)


def _messages_url(base_url: str | None) -> str:
    """把根地址、版本化 Base URL 或完整端点统一成 Messages API 地址。"""
    if not base_url:
        return _API_URL
    normalized = base_url.rstrip("/")
    if normalized.endswith("/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


def _rejects_effort(body: str) -> bool:
    low = body.lower()
    return any(marker in low for marker in _EFFORT_REJECT_MARKERS)


def _content_payload(block: ContentBlock) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text} if block.text else None
    if isinstance(block, ThinkingBlock):
        # 签名必须原样带回，否则回放这轮会被拒
        out: dict[str, Any] = {"type": "thinking", "thinking": block.text}
        if block.signature:
            out["signature"] = block.signature
        return out
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.mime,
                "data": base64.b64encode(block.data).decode("ascii"),
            },
        }
    if not isinstance(block, ToolResultBlock):  # 防御：将来加了新块类型别静默塞错形状
        raise TypeError(f"unsupported content block: {type(block).__name__}")
    # 图片作为工具结果内容的一部分，这是 Anthropic 原生支持的形状（OpenAI 侧做不到）
    content: list[dict[str, Any]] = [{"type": "text", "text": block.content}]
    for img in block.images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.mime,
                    "data": base64.b64encode(img.data).decode("ascii"),
                },
            }
        )
    out = {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": content}
    if block.is_error:
        out["is_error"] = True
    return out


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    supports_tools = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        user_agent: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        max_attempts: int = 3,
    ) -> None:
        self._api_key = api_key
        self._max_attempts = max(1, max_attempts)
        self._api_url = _messages_url(base_url)
        self._user_agent = user_agent.strip() if user_agent else None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"x-api-key": self._api_key, "anthropic-version": _API_VERSION}
        if self._user_agent:
            headers["user-agent"] = self._user_agent
        return headers

    @staticmethod
    def _payload(
        messages: Sequence[Message],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        # Anthropic 的 system 提示是顶层参数，不在 messages 里
        system_parts = [m.text for m in messages if m.role == "system"]
        payload_messages: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if isinstance(m.content, str):
                payload_messages.append({"role": m.role, "content": m.content})
                continue
            parts = [p for p in (_content_payload(b) for b in m.content) if p is not None]
            payload_messages.append({"role": m.role, "content": parts})

        if images and payload_messages:
            target = next(
                (m for m in reversed(payload_messages) if m["role"] == "user"),
                payload_messages[-1],
            )
            parts = (
                target["content"]
                if isinstance(target["content"], list)
                else [{"type": "text", "text": target["content"]}]
            )
            for image in images:
                parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(image).decode("ascii"),
                        },
                    }
                )
            target["content"] = parts

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": payload_messages,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if effort is not None:
            # Anthropic 的推理档位在 output_config 里，不是顶层参数
            payload["output_config"] = {"effort": effort}
        if tools:
            # ToolSpec.input_schema 直接就是这里要的 input_schema，零转换
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
            if tool_choice == "none":
                payload["tool_choice"] = {"type": "none"}
            elif tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
            elif tool_choice == "auto":
                payload["tool_choice"] = {"type": "auto"}
        return payload

    async def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        """429 / 529 / 5xx / 网络错误重试，其余状态原样返回给调用方判断。

        「不支持 effort」不在重试之列：那不是临时故障，交回 complete 去掉参数再发。
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                resp = await self._client.post(self._api_url, headers=self._headers(), json=payload)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            if (
                "output_config" in payload
                and resp.status_code >= 400
                and _rejects_effort(resp.text)
            ):
                return resp
            if resp.status_code in _RETRYABLE_STATUS and attempt < self._max_attempts - 1:
                delay = _retry_delay(resp, attempt)
                logger.warning(
                    "anthropic %s，%.0fs 后重试（%d/%d）",
                    resp.status_code,
                    delay,
                    attempt + 1,
                    self._max_attempts,
                )
                await asyncio.sleep(delay)
                continue
            return resp
        raise RuntimeError(
            f"anthropic 请求 {self._api_url} 重试 {self._max_attempts} 次后仍失败："
            f"{type(last_exc).__name__}: {last_exc}"
        )

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> CompletionResult:
        resp = await self._post_with_retry(
            self._payload(
                messages,
                model,
                temperature,
                max_tokens,
                stream=False,
                images=images,
                effort=effort,
                tools=tools,
                tool_choice=tool_choice,
            )
        )
        if effort is not None and resp.status_code >= 400 and _rejects_effort(resp.text):
            # 该模型不认这个档位：去掉参数重试一次，别让配错档位打断整个环节
            logger.warning("模型 %s 不支持 effort=%s，已去掉该参数重试", model, effort)
            resp = await self._post_with_retry(
                self._payload(
                    messages,
                    model,
                    temperature,
                    max_tokens,
                    stream=False,
                    images=images,
                    tools=tools,
                    tool_choice=tool_choice,
                )
            )
        resp.raise_for_status()
        data = resp.json()
        blocks: list[ContentBlock] = []
        texts: list[str] = []
        for raw in data.get("content", []):
            kind = raw.get("type")
            if kind == "text":
                texts.append(raw.get("text", ""))
                blocks.append(TextBlock(raw.get("text", "")))
            elif kind == "thinking":
                blocks.append(ThinkingBlock(raw.get("thinking", ""), raw.get("signature")))
            elif kind == "tool_use":
                blocks.append(
                    ToolUseBlock(
                        str(raw.get("id") or ""),
                        str(raw.get("name") or ""),
                        raw.get("input") or {},
                    )
                )
        return CompletionResult(
            content="".join(texts),
            model=data.get("model", model),
            finish_reason=normalize_finish_reason(data.get("stop_reason")),
            usage=normalize_usage(data.get("usage")),
            blocks=tuple(blocks),
        )

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
    ) -> AsyncIterator[str]:
        """纯文本流式：``stream_events`` 的过滤器（只有一份 SSE 解析）。"""
        async for ev in self.stream_events(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            images=images,
            effort=effort,
        ):
            if isinstance(ev, TextDelta):
                yield ev.text

    async def stream_events(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """按 index 归并的结构化流式。

        Anthropic 的形状是 ``content_block_start``（index + tool_use 的 id/name）→
        ``content_block_delta``（``input_json_delta.partial_json`` 逐段拼参数）→
        ``content_block_stop``。usage 分两处给：``message_start`` 给输入，``message_delta``
        给输出——此前这两处都没解析（代码里就写着 TODO）。
        """
        payload = self._payload(
            messages,
            model,
            temperature,
            max_tokens,
            stream=True,
            images=images,
            effort=effort,
            tools=tools,
            tool_choice=tool_choice,
        )
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        # 只在拿到第一个事件之前重试：开流就 429 / 529 / 断连时退避再开；事件已经在
        # 往外吐时出错不能重来——调用方已经收到的那部分会被重复一遍
        started = False
        for attempt in range(self._max_attempts):
            delay = _BACKOFF_BASE_SECONDS * (2**attempt)
            try:
                async with self._client.stream(
                    "POST", self._api_url, headers=self._headers(), json=payload
                ) as resp:
                    if resp.status_code in _RETRYABLE_STATUS and attempt < self._max_attempts - 1:
                        await resp.aread()
                        delay = _retry_delay(resp, attempt)
                        logger.warning(
                            "anthropic stream %s，%.0fs 后重试（%d/%d）",
                            resp.status_code,
                            delay,
                            attempt + 1,
                            self._max_attempts,
                        )
                    else:
                        resp.raise_for_status()
                        started = True
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            event = json.loads(line[len("data:") :].strip())
                            kind = event.get("type")
                            if kind == "message_start":
                                usage.update((event.get("message") or {}).get("usage") or {})
                            elif kind == "content_block_start":
                                block = event.get("content_block") or {}
                                if block.get("type") == "tool_use":
                                    yield ToolUseStart(
                                        int(event.get("index", 0)),
                                        str(block.get("id") or ""),
                                        str(block.get("name") or ""),
                                    )
                            elif kind == "content_block_delta":
                                delta = event.get("delta") or {}
                                dtype = delta.get("type")
                                if dtype == "input_json_delta":
                                    yield ToolUseArgsDelta(
                                        int(event.get("index", 0)), delta.get("partial_json") or ""
                                    )
                                elif dtype == "thinking_delta":
                                    yield ThinkingDelta(delta.get("thinking") or "")
                                elif dtype == "signature_delta":
                                    yield ThinkingDelta("", delta.get("signature"))
                                elif text := delta.get("text"):
                                    yield TextDelta(text)
                            elif kind == "content_block_stop":
                                yield ToolUseStop(int(event.get("index", 0)))
                            elif kind == "message_delta":
                                usage.update(event.get("usage") or {})
                                if reason := (event.get("delta") or {}).get("stop_reason"):
                                    finish_reason = reason
                        break
            except (httpx.TransportError, httpx.TimeoutException):
                if started or attempt >= self._max_attempts - 1:
                    raise
                logger.warning(
                    "anthropic stream connection failed, retrying (%d/%d)",
                    attempt + 1,
                    self._max_attempts,
                )
            await asyncio.sleep(delay)
        yield StreamDone(
            finish_reason=normalize_finish_reason(finish_reason), usage=normalize_usage(usage)
        )

    async def aclose(self) -> None:
        await self._client.aclose()
