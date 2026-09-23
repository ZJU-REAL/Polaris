"""OpenAI Responses API provider（``POST {base_url}/responses``，#809）。

越来越多的模型与网关优先（甚至只）提供 Responses API；部分新模型在 Chat Completions
接口下拿不全 reasoning / tool calling / streaming。这里把它作为一种**协议**加进来，
由配置的人显式选择，而不是按模型名猜——同一个模型在不同网关上可能暴露不同接口。

与 Chat Completions 同属 OpenAI 一族：鉴权、重试、``/embeddings``、rerank 都一样，
所以继承 :class:`OpenAICompatProvider`，只重写对话协议那一层。

协议映射（Chat → Responses）：
- 消息 → ``input`` 数组，按原顺序；system 原样保留角色（Responses 接受 system/developer
  角色的输入项），不挪进 ``instructions``——挪了会打乱多条 system 消息的相对位置。
- user 文本 → ``input_text``；图片 → ``input_image``（data URL）。
- assistant 文本 → ``output_text``；它发起的工具调用 → 顶层 ``function_call`` 项。
- 工具结果 → 顶层 ``function_call_output`` 项。
- 工具定义扁平化：``{type:"function", name, description, parameters}``，没有 Chat
  那一层 ``function`` 包装。
- 推理档位 → ``reasoning.effort``；usage 的 ``input/output_tokens`` 归一成平台口径。
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.core.llm.base import (
    CompletionResult,
    ContentBlock,
    EffortLevel,
    ImageBlock,
    Message,
    StreamDone,
    StreamEvent,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolResultBlock,
    ToolsUnsupportedError,
    ToolUseArgsDelta,
    ToolUseBlock,
    ToolUseStart,
    ToolUseStop,
    normalize_usage,
)
from app.core.llm.openai_compat import (
    _FORCE_STREAM_MARKER,
    OpenAICompatProvider,
    _EffortUnsupported,
    _rejects_effort,
    _tools_unsupported,
)

logger = logging.getLogger(__name__)

#: 没给 max_tokens 时的输出上限。与 Chat Completions 那边的缺省一致：
#: 部分网关强制要求这个参数，缺了直接 400。
_DEFAULT_MAX_OUTPUT_TOKENS = 8192


def _image_part(data: bytes, mime: str = "image/png") -> dict[str, Any]:
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"}


def _input_items(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Polaris 消息 → Responses 的 ``input`` 数组。

    工具调用与工具结果在 Responses 里是**顶层项**，不属于任何一条消息，所以一条
    assistant 消息可能拆成「一条文本消息 + 若干 function_call 项」。顺序保持不变：
    模型要看到的是「先说了什么、再调了什么、再拿到什么」。
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.role
        text_parts: list[dict[str, Any]] = []

        def flush(role: str = role, parts: list[dict[str, Any]] = text_parts) -> None:
            if parts:
                items.append({"role": role, "content": list(parts)})
                parts.clear()

        for block in message.blocks:
            if isinstance(block, TextBlock):
                kind = "output_text" if role == "assistant" else "input_text"
                text_parts.append({"type": kind, "text": block.text})
            elif isinstance(block, ImageBlock):
                text_parts.append(_image_part(block.data, block.mime))
            elif isinstance(block, ToolUseBlock):
                flush()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    }
                )
            elif isinstance(block, ToolResultBlock):
                flush()
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": block.content,
                    }
                )
            elif isinstance(block, ThinkingBlock):
                # 回放推理需要服务端给的 reasoning 项 id / 加密内容，一段摘要文本回灌
                # 不回去；原样丢掉比塞一条伪造的 reasoning 项安全
                continue
        flush()
    return items


def _tools_payload(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具定义：Responses 是扁平的，没有 Chat 那一层 ``function`` 包装。"""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("parameters") or {"type": "object", "properties": {}},
        }
        for t in tools
    ]


def _tool_choice_payload(tool_choice: str) -> Any:
    """``auto`` / ``none`` / ``required`` 原样；其余当作要强制调用的工具名。"""
    if tool_choice in ("auto", "none", "required"):
        return tool_choice
    return {"type": "function", "name": tool_choice}


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """工具参数解析不了不抛：交给上层告诉模型重发，抛出去会把整轮打断。"""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"__parse_error__": str(raw)[:2000]}
    return parsed if isinstance(parsed, dict) else {"__parse_error__": str(raw)[:2000]}


def _finish_reason(
    status: str | None, incomplete: dict[str, Any] | None, *, tool_calls: bool
) -> str:
    """Responses 没有 finish_reason，要从状态和输出里推。

    有工具调用就是 ``tool_use``——上层靠它决定「执行工具再来一轮」，漏判的话工具
    调用会被当成最终回答。
    """
    if tool_calls:
        return "tool_use"
    if status == "incomplete" and (incomplete or {}).get("reason") == "max_output_tokens":
        return "max_tokens"
    return "stop"


def _blocks_from_output(output: list[dict[str, Any]]) -> tuple[str, list[ContentBlock]]:
    """非流式响应的 ``output`` 数组 → (拼好的文本, 块列表)。"""
    text_chunks: list[str] = []
    blocks: list[ContentBlock] = []
    for item in output or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "refusal"):
                    piece = part.get("text") or part.get("refusal") or ""
                    if piece:
                        text_chunks.append(piece)
                        blocks.append(TextBlock(piece))
        elif kind == "function_call":
            blocks.append(
                ToolUseBlock(
                    str(item.get("call_id") or item.get("id") or ""),
                    str(item.get("name") or ""),
                    _parse_arguments(item.get("arguments")),
                )
            )
        elif kind == "reasoning":
            summary = "".join(
                s.get("text") or "" for s in item.get("summary") or [] if isinstance(s, dict)
            )
            if summary:
                blocks.append(ThinkingBlock(summary))
    return "".join(text_chunks), blocks


class OpenAIResponsesProvider(OpenAICompatProvider):
    """OpenAI Responses API。鉴权、重试、embeddings、rerank 继承自 Chat Completions 那边。"""

    name = "openai_responses"
    supports_tools = True

    def _responses_payload(
        self,
        messages: Sequence[Message],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        *,
        stream: bool,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        items = _input_items(messages)
        if images:
            # 多模态：图片附在最后一条 user 消息上，与 Chat Completions 那边一致
            target = next((i for i in reversed(items) if i.get("role") == "user"), None)
            if target is None:
                target = {"role": "user", "content": []}
                items.append(target)
            target["content"].extend(_image_part(image) for image in images)
        payload: dict[str, Any] = {
            "model": model,
            "input": items,
            "stream": stream,
            "max_output_tokens": max_tokens
            if max_tokens is not None
            else _DEFAULT_MAX_OUTPUT_TOKENS,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if effort is not None and model not in self._effort_unsupported:
            payload["reasoning"] = {"effort": effort}
        if tools:
            payload["tools"] = _tools_payload(tools)
            if tool_choice is not None:
                payload["tool_choice"] = _tool_choice_payload(tool_choice)
        return payload

    # ---- 一次性 ----

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
        try:
            return await self._complete_responses(
                messages, model, temperature, max_tokens, images, effort, tools, tool_choice
            )
        except _EffortUnsupported as e:
            # 该模型不吃这个档位：去掉参数重试一次，别让配错档位打断整个环节
            self._effort_unsupported.add(model)
            logger.warning("模型 %s 不支持 effort=%s，已去掉该参数重试：%s", model, effort, e)
            return await self._complete_responses(
                messages, model, temperature, max_tokens, images, None, tools, tool_choice
            )

    async def _complete_responses(
        self,
        messages: Sequence[Message],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        images: list[bytes] | None,
        effort: EffortLevel | None,
        tools: Sequence[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> CompletionResult:
        payload = self._responses_payload(
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
        url = f"{self._base_url}/responses"
        resp = await self._post_with_retry(url, payload)
        if resp.status_code >= 400:
            body = resp.text[:500]
            if effort is not None and _rejects_effort(body):
                raise _EffortUnsupported(body)
            if resp.status_code == 400 and tools and _tools_unsupported(body):
                raise ToolsUnsupportedError(body)
            if resp.status_code == 400 and _FORCE_STREAM_MARKER in body.lower():
                logger.info("openai_responses %s 仅支持流式，自动改用流式聚合：%s", url, model)
                return await self._aggregate(self._events({**payload, "stream": True}), model)
            raise RuntimeError(f"openai_responses {resp.status_code} from {url}: {body}")
        data = resp.json()
        text, blocks = _blocks_from_output(data.get("output") or [])
        if not text and isinstance(data.get("output_text"), str):
            # 有的网关只给便捷字段 output_text、不给结构化 output
            text = data["output_text"]
            blocks = [TextBlock(text), *blocks]
        return CompletionResult(
            content=text,
            model=data.get("model", model),
            finish_reason=_finish_reason(
                data.get("status"),
                data.get("incomplete_details"),
                tool_calls=any(isinstance(b, ToolUseBlock) for b in blocks),
            ),
            usage=normalize_usage(data.get("usage")),
            blocks=tuple(blocks),
        )

    async def _aggregate(self, events: AsyncIterator[StreamEvent], model: str) -> CompletionResult:
        """把事件流聚合回一次性结果（强制流式的网关用）。"""
        text: list[str] = []
        thinking: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        done = StreamDone()
        async for event in events:
            if isinstance(event, TextDelta):
                text.append(event.text)
            elif isinstance(event, ThinkingDelta):
                thinking.append(event.text)
            elif isinstance(event, ToolUseStart):
                calls.setdefault(event.index, {"id": event.id, "name": event.name, "args": ""})
            elif isinstance(event, ToolUseArgsDelta):
                calls.setdefault(event.index, {"id": "", "name": "", "args": ""})
                calls[event.index]["args"] += event.json_fragment
            elif isinstance(event, StreamDone):
                done = event
        blocks: list[ContentBlock] = []
        if thinking:
            blocks.append(ThinkingBlock("".join(thinking)))
        content = "".join(text)
        if content:
            blocks.append(TextBlock(content))
        for index in sorted(calls):
            call = calls[index]
            blocks.append(ToolUseBlock(call["id"], call["name"], _parse_arguments(call["args"])))
        return CompletionResult(
            content=content,
            model=model,
            finish_reason=done.finish_reason,
            usage=dict(done.usage),
            blocks=tuple(blocks),
        )

    # ---- 流式 ----

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
    ) -> AsyncIterator[str]:
        """只要文本的流式：复用 :meth:`stream_events`，只有一份 SSE 解析。"""
        async for event in self.stream_events(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            images=images,
            effort=effort,
        ):
            if isinstance(event, TextDelta):
                yield event.text

    async def stream_events(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload = self._responses_payload(
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
        try:
            async for event in self._events(payload):
                yield event
        except _EffortUnsupported as e:
            self._effort_unsupported.add(model)
            logger.warning("模型 %s 不支持 effort=%s，已去掉该参数重试：%s", model, effort, e)
            payload.pop("reasoning", None)
            async for event in self._events(payload):
                yield event

    async def _events(self, payload: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        """流式请求 /responses，把服务端事件翻成平台的 StreamEvent。

        Responses 的流以 ``response.completed`` 收尾，没有 Chat 那种 ``[DONE]``
        哨兵；个别网关仍会补一个，遇到也当结束。
        """
        url = f"{self._base_url}/responses"
        saw_tool_call = False
        finished = False
        async with self._client.stream("POST", url, headers=self._headers(), json=payload) as resp:
            if resp.status_code >= 400:
                # 流式响应体要显式读出来才能看到错误内容；不要用 raise_for_status()，
                # 它只带状态码，把上游写明的原因整段丢掉
                body = (await resp.aread()).decode(errors="replace")[:500]
                if payload.get("reasoning") is not None and _rejects_effort(body):
                    raise _EffortUnsupported(body)
                if resp.status_code == 400 and payload.get("tools") and _tools_unsupported(body):
                    raise ToolsUnsupportedError(body)
                raise RuntimeError(f"openai_responses {resp.status_code} from {url}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # "event:" 行只是类型的重复，类型在 data 里也有
                chunk = line[len("data:") :].strip()
                if not chunk:
                    continue
                if chunk == "[DONE]":
                    break
                event = json.loads(chunk)
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    if delta := event.get("delta"):
                        yield TextDelta(delta)
                elif kind in (
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                ):
                    if delta := event.get("delta"):
                        yield ThinkingDelta(delta)
                elif kind == "response.output_item.added":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        saw_tool_call = True
                        yield ToolUseStart(
                            int(event.get("output_index", 0) or 0),
                            str(item.get("call_id") or item.get("id") or ""),
                            str(item.get("name") or ""),
                        )
                elif kind == "response.function_call_arguments.delta":
                    yield ToolUseArgsDelta(
                        int(event.get("output_index", 0) or 0), str(event.get("delta") or "")
                    )
                elif kind == "response.output_item.done":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        yield ToolUseStop(int(event.get("output_index", 0) or 0))
                elif kind in ("response.completed", "response.incomplete"):
                    response = event.get("response") or {}
                    finished = True
                    yield StreamDone(
                        finish_reason=_finish_reason(
                            response.get("status"),
                            response.get("incomplete_details"),
                            tool_calls=saw_tool_call,
                        ),
                        usage=normalize_usage(response.get("usage")),
                    )
                    break
                elif kind in ("response.failed", "error"):
                    detail = (
                        (event.get("response") or {}).get("error") or event.get("error") or event
                    )
                    raise RuntimeError(f"openai_responses stream failed at {url}: {detail}")
        if not finished:
            # 连接正常关掉、却没给 response.completed：多半是网关截断。如实收尾，
            # 而不是让上层永远等一个不会来的 StreamDone
            yield StreamDone(finish_reason="tool_use" if saw_tool_call else "stop")
