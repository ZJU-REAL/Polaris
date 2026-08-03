"""Anthropic Messages API Provider，基于 httpx（不依赖官方 SDK）。

complete / stream / stream_events 三个接口完整；缓存与重试仍留 TODO。
"""

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
)

logger = logging.getLogger("polaris.llm")

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096
# 老模型不认 output_config.effort（该参数只在 4.6+ 上 GA）；命中即去掉重试一次
_EFFORT_REJECT_MARKERS = ("effort", "output_config")


def _rejects_effort(body: str) -> bool:
    low = body.lower()
    return any(marker in low for marker in _EFFORT_REJECT_MARKERS)


def _normalize_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    """Anthropic 的 usage 键名归一成平台口径。

    它给的是 ``input_tokens`` / ``output_tokens``，而记账读的是 ``prompt_tokens`` /
    ``completion_tokens``，读不到就按 len/4 估——**所以此前走 Anthropic 的用量统计全是
    估算值**。归一化放在 provider 里做，不污染 router。
    """
    if not raw:
        return {}
    usage = dict(raw)
    if "input_tokens" in usage:
        usage["prompt_tokens"] = int(usage["input_tokens"])
    if "output_tokens" in usage:
        usage["completion_tokens"] = int(usage["output_tokens"])
    if "prompt_tokens" in usage and "completion_tokens" in usage:
        usage.setdefault("total_tokens", usage["prompt_tokens"] + usage["completion_tokens"])
    return {k: v for k, v in usage.items() if isinstance(v, int)}


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
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
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
        # TODO(M2): 重试/限速/错误分类
        resp = await self._client.post(
            _API_URL,
            headers=self._headers(),
            json=self._payload(
                messages,
                model,
                temperature,
                max_tokens,
                stream=False,
                images=images,
                effort=effort,
                tools=tools,
                tool_choice=tool_choice,
            ),
        )
        if effort is not None and resp.status_code >= 400 and _rejects_effort(resp.text):
            # 该模型不认这个档位：去掉参数重试一次，别让配错档位打断整个环节
            logger.warning("模型 %s 不支持 effort=%s，已去掉该参数重试", model, effort)
            resp = await self._client.post(
                _API_URL,
                headers=self._headers(),
                json=self._payload(
                    messages,
                    model,
                    temperature,
                    max_tokens,
                    stream=False,
                    images=images,
                    tools=tools,
                    tool_choice=tool_choice,
                ),
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
            usage=_normalize_usage(data.get("usage")),
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
        async with self._client.stream(
            "POST", _API_URL, headers=self._headers(), json=payload
        ) as resp:
            resp.raise_for_status()
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
        yield StreamDone(
            finish_reason=normalize_finish_reason(finish_reason), usage=_normalize_usage(usage)
        )

    async def aclose(self) -> None:
        await self._client.aclose()
