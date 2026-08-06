"""OpenAI 兼容接口 Provider（DeepSeek / vLLM / OpenRouter 等），基于 httpx。

429/5xx 自动指数退避重试（尊重 Retry-After）；tool-use 留 TODO。
"""

import asyncio
import base64
import json
import logging
import math
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.llm.base import (
    CompletionResult,
    EffortLevel,
    ImageBlock,
    LLMProvider,
    Message,
    RerankResult,
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
    normalize_finish_reason,
)

logger = logging.getLogger("polaris.llm")

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 3.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 部分中转强制流式：非流式请求返回 400 + 该提示，此时自动改走流式并聚合
_FORCE_STREAM_MARKER = "stream must be set to true"
# 模型/中转不认 reasoning_effort（非推理模型、老版中转、或该模型不支持所配档位）时的
# 错误特征。命中即去掉该参数重试一次——配错档位不该让整个环节挂掉。
# LiteLLM 之类的中转会把 UnsupportedParamsError 包成 400 之外的状态码抛出来，
# 所以这里只认错误内容、不认状态码；调用方已保证"本次确实发了该参数"。
_EFFORT_REJECT_MARKERS = ("reasoning_effort", "reasoning.effort", "effort")


class _EffortUnsupported(RuntimeError):
    """服务端明确因 reasoning_effort 拒绝了请求；调用方去掉该参数重试。"""


#: 中转/本地推理服务不支持 tools 时的说法。命中就降级回无工具的一次性问答，
#: 而不是把这轮打成失败——用户要的是答案，不是「你的服务端不支持函数调用」。
_TOOLS_REJECT_MARKERS = (
    "tools is not supported",
    "tool_choice",
    "does not support tools",
    "function calling",
    "functions are not supported",
    "unsupported parameter: 'tools'",
)


def _tools_unsupported(body: str) -> bool:
    low = body.lower()
    return any(marker in low for marker in _TOOLS_REJECT_MARKERS)


def _rejects_effort(body: str) -> bool:
    """错误信息提到 effort —— 仅在本次确实发了该参数时才做此判断。"""
    low = body.lower()
    return any(marker in low for marker in _EFFORT_REJECT_MARKERS)


_MATRYOSHKA_UNSUPPORTED_MARKER = "does not support matryoshka representation"
# PostgreSQL 的 papers/paper_chunks/ideas 向量列统一为 vector(1024)。
_POLARIS_EMBEDDING_DIM = 1024


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return min(60.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return _BACKOFF_BASE_SECONDS * (2**attempt)


def _is_qwen3_embedding(model: str) -> bool:
    """Qwen3 Embedding 支持 MRL，但旧版 vLLM 可能没有从模型配置识别出来。"""
    normalized = model.lower().replace("-", "_")
    return "qwen3" in normalized and "embedding" in normalized


def _truncate_and_normalize(vector: list[float], dimensions: int) -> list[float]:
    """按 Matryoshka 语义截取前 N 维，并恢复单位长度。"""
    if len(vector) < dimensions:
        raise RuntimeError(
            f"embedding dimension mismatch: expected {dimensions}, got {len(vector)}"
        )
    truncated = vector[:dimensions]
    norm = math.sqrt(math.fsum(value * value for value in truncated))
    if norm == 0:
        raise RuntimeError("cannot normalize a zero embedding vector")
    return [value / norm for value in truncated]


def _messages_payload(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """把消息（可能带 content blocks）翻成 OpenAI 的形状。

    两处不对称，都是 OpenAI 侧的限制，不是我们的选择：

    - **工具结果要扇出**：我们把一轮里的 K 个结果装在一条 ``role="user"`` 消息里
      （Anthropic 形状），这里要拆成 K 条 ``role="tool"``。
    - **``role="tool"`` 塞不进图片**（多数中转只认纯文本）。所以图片另起一条合成的
      user 消息，正文写明它属于哪次调用——绑定关系靠文字，是有损的，但没有更好的办法。
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
            continue

        blocks = list(m.content)
        tool_results = [b for b in blocks if isinstance(b, ToolResultBlock)]
        tool_uses = [b for b in blocks if isinstance(b, ToolUseBlock)]
        texts = [b.text for b in blocks if isinstance(b, TextBlock | ThinkingBlock)]
        images = [b for b in blocks if isinstance(b, ImageBlock)]

        if tool_results:
            for result in tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.tool_use_id,
                        "content": result.content,
                    }
                )
            carried = [img for r in tool_results for img in r.images] + images
            if carried:
                parts: list[dict[str, Any]] = [
                    {"type": "text", "text": "以下是上面工具调用返回的图片。"}
                ]
                for img in carried:
                    b64 = base64.b64encode(img.data).decode("ascii")
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{img.mime};base64,{b64}"},
                        }
                    )
                out.append({"role": "user", "content": parts})
            continue

        entry: dict[str, Any] = {"role": m.role, "content": "\n".join(texts)}
        if tool_uses:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.input, ensure_ascii=False),
                    },
                }
                for call in tool_uses
            ]
            # 带 tool_calls 时 content 允许为空；有的中转对空字符串更宽容，保持 ""
        if images:
            parts = [{"type": "text", "text": entry["content"]}]
            for img in images:
                b64 = base64.b64encode(img.data).decode("ascii")
                parts.append(
                    {"type": "image_url", "image_url": {"url": f"data:{img.mime};base64,{b64}"}}
                )
            entry["content"] = parts
        out.append(entry)
    return out


def _tool_call_events(delta: dict[str, Any]) -> list[StreamEvent]:
    """把一个 chunk 里的 ``delta.tool_calls[]`` 翻成事件。

    中转的三个常见差异都在这里兜住：单工具时可能**不发 index**（一律兜底 0）；``id`` /
    ``name`` 有的只在首片发、有的每片都发（首次非空即锁定，交给累加器判重）；
    ``arguments`` 是逐段的 JSON 字符串，这里只往下传，不解析。
    """
    events: list[StreamEvent] = []
    for frag in delta.get("tool_calls") or []:
        index = int(frag.get("index", 0) or 0)
        fn = frag.get("function") or {}
        if frag.get("id") or fn.get("name"):
            events.append(ToolUseStart(index, str(frag.get("id") or ""), str(fn.get("name") or "")))
        if (args := fn.get("arguments")) is not None:
            events.append(ToolUseArgsDelta(index, args))
    return events


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"
    supports_tools = True

    async def _post_with_retry(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """429/5xx/网络错误重试（指数退避，尊重 Retry-After），其余状态原样返回。"""
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                resp = await self._client.post(url, headers=self._headers(), json=payload)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            if (
                resp.status_code in _RETRYABLE_STATUS
                and payload.get("reasoning_effort") is not None
                and _rejects_effort(resp.text)
            ):
                # 中转把「不支持该参数」包成了 5xx：这不是临时故障，重试多少次都一样，
                # 直接交回上层去掉参数重试，别白烧几十秒退避。
                return resp
            if resp.status_code in _RETRYABLE_STATUS and attempt < self._max_attempts - 1:
                delay = _retry_delay(resp, attempt)
                logger.warning(
                    "openai_compat %s，%.0fs 后重试（%d/%d）：%s",
                    resp.status_code,
                    delay,
                    attempt + 1,
                    self._max_attempts,
                    url,
                )
                await asyncio.sleep(delay)
                continue
            return resp
        raise RuntimeError(
            f"openai_compat 请求 {url} 重试 {self._max_attempts} 次后仍失败：{last_exc}"
        )

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_attempts = max_attempts
        self._client = client or httpx.AsyncClient(timeout=timeout)
        # 已确认不吃 reasoning_effort 的 model（本进程内记忆）。provider 实例被
        # LLMRouter 缓存复用，所以每个模型只需要付一次"失败再重试"的往返代价。
        self._effort_unsupported: set[str] = set()

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
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        payload_messages: list[dict[str, Any]] = _messages_payload(messages)
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
        if effort is not None and model not in self._effort_unsupported:
            # 推理模型的思考深度；非推理模型/中转不认时不要发
            payload["reasoning_effort"] = effort
        # Anthropic 系模型（经 LiteLLM 等代理）强制要求 max_tokens，缺省给足额度
        payload["max_tokens"] = max_tokens if max_tokens is not None else 8192
        if tools:
            # ToolSpec.input_schema 本来就是标准 JSON Schema，这里零转换
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
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
        try:
            return await self._complete_once(
                messages, model, temperature, max_tokens, images, effort, tools, tool_choice
            )
        except _EffortUnsupported as e:
            # 该模型不吃这个档位：去掉参数重试一次，别让配错档位打断整个环节
            self._effort_unsupported.add(model)
            logger.warning("模型 %s 不支持 effort=%s，已去掉该参数重试：%s", model, effort, e)
            return await self._complete_once(
                messages, model, temperature, max_tokens, images, None, tools, tool_choice
            )

    async def _complete_once(
        self,
        messages: Sequence[Message],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        images: list[bytes] | None,
        effort: EffortLevel | None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> CompletionResult:
        payload = self._payload(
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
        resp = await self._post_with_retry(f"{self._base_url}/chat/completions", payload)
        if resp.status_code >= 400:
            body = resp.text[:500]
            if effort is not None and _rejects_effort(body):
                raise _EffortUnsupported(body)
            if resp.status_code == 400 and _FORCE_STREAM_MARKER in body.lower():
                # 强制流式的中转：自动改用流式请求并聚合成非流式结果
                logger.info(
                    "openai_compat %s 仅支持流式，自动改用流式聚合：%s", self._base_url, model
                )
                return await self._complete_via_stream({**payload, "stream": True})
            raise RuntimeError(f"openai_compat {resp.status_code} from {self._base_url}: {body}")
        data = resp.json()
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        blocks: list[Any] = [TextBlock(text)] if text else []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, ValueError):
                # 解析不了不抛：交给上层告诉模型重发，抛出去会把整轮打断
                parsed = {"__parse_error__": str(raw_args)[:2000]}
            call_id = str(call.get("id") or "")
            blocks.append(ToolUseBlock(call_id, str(fn.get("name") or ""), parsed))
        return CompletionResult(
            content=text,
            model=data.get("model", model),
            finish_reason=normalize_finish_reason(choice.get("finish_reason")),
            usage=data.get("usage") or {},
            blocks=tuple(blocks),
        )

    async def _stream_chunks(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """流式请求 chat/completions，逐个 yield 解析后的 SSE chunk（dict）。

        stream() 与强制流式回退（_complete_via_stream）共用这一份 SSE 解析。
        """
        async with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                # 流式响应体要显式读出来才能看到错误内容
                body = (await resp.aread()).decode(errors="replace")[:500]
                if payload.get("reasoning_effort") is not None and _rejects_effort(body):
                    raise _EffortUnsupported(body)
                if resp.status_code == 400 and _tools_unsupported(body):
                    # 这个中转不认 tools：交给上层降级，而不是把这轮打成失败
                    raise ToolsUnsupportedError(body)
                # **不要用 raise_for_status()**：httpx 的那个异常只带状态码，把上游
                # 已经写明的原因（哪个参数不合法、哪个模型不存在）整段丢掉，用户拿到
                # 的就只有一句「400 Bad Request」，我们也无从查起。
                raise RuntimeError(
                    f"openai_compat {resp.status_code} from {self._base_url}: {body}"
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if chunk == "[DONE]":
                    break
                yield json.loads(chunk)

    async def _complete_via_stream(self, payload: dict[str, Any]) -> CompletionResult:
        """流式聚合：拼接所有 delta.content；usage 取带 usage 的 chunk（通常最后一个）。"""
        parts: list[str] = []
        usage: dict[str, int] = {}
        model_name: str = payload["model"]
        finish_reason: str | None = None
        async for data in self._stream_chunks(payload):
            if data.get("model"):
                model_name = data["model"]
            choices = data.get("choices") or []
            if choices:
                if content := (choices[0].get("delta") or {}).get("content"):
                    parts.append(content)
                if reason := choices[0].get("finish_reason"):
                    finish_reason = reason
            if data.get("usage"):
                usage = data["usage"]
        return CompletionResult(
            content="".join(parts),
            model=model_name,
            finish_reason=finish_reason,
            usage=usage,
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
        """纯文本流式：``stream_events`` 的过滤器。

        保持这个签名不变（三个 SSE 端点 + 写作辅助都在用），也不给它加 tools——
        只有一份 SSE 解析，就是下面那个。
        """
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
        emitted = False
        try:
            async for ev in self._events_from(payload):
                emitted = emitted or isinstance(ev, TextDelta | ToolUseStart)
                yield ev
        except _EffortUnsupported as e:
            # effort 是在首个 token 之前被拒的，重试不会重复输出。
            # 判据里必须带上 ToolUseStart：已经发起过工具调用还重试，会重复调用 + 双倍计费。
            if emitted:
                raise
            self._effort_unsupported.add(model)
            logger.warning("模型 %s 不支持 effort=%s，已去掉该参数重试：%s", model, effort, e)
            payload.pop("reasoning_effort", None)
            async for ev in self._events_from(payload):
                yield ev

    async def _events_from(self, payload: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        """一个 chunk 一个 chunk 地翻成结构化事件。

        ``delta.content`` 与 ``delta.tool_calls`` **可能出现在同一个 chunk 里**，所以这里
        不能写成 if/elif —— 那样会丢掉其中一个。
        """
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        open_indexes: set[int] = set()
        async for data in self._stream_chunks(payload):
            if data.get("usage"):
                usage = data["usage"]
            choices = data.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            if content := delta.get("content"):
                yield TextDelta(content)
            # DeepSeek 与部分中转的非标字段；有就映射成思考，没有也不强求
            if reasoning := (delta.get("reasoning_content") or delta.get("reasoning")):
                yield ThinkingDelta(reasoning)
            for ev in _tool_call_events(delta):
                if isinstance(ev, ToolUseStart):
                    open_indexes.add(ev.index)
                yield ev
            if reason := choice.get("finish_reason"):
                finish_reason = reason
        for index in sorted(open_indexes):
            yield ToolUseStop(index)
        yield StreamDone(finish_reason=normalize_finish_reason(finish_reason), usage=usage)

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        url = f"{self._base_url}/embeddings"
        payload: dict[str, Any] = {"model": model, "input": texts}
        qwen3_embedding = _is_qwen3_embedding(model)
        if qwen3_embedding:
            # Qwen3 Embedding 原生支持 32..hidden_size 的 MRL 输出维度。新 vLLM
            # 会直接返回 1024 维；旧部署若没有标记 is_matryoshka，会在下面兼容。
            payload["dimensions"] = _POLARIS_EMBEDDING_DIM

        resp = await self._post_with_retry(url, payload)
        if (
            qwen3_embedding
            and resp.status_code == 400
            and _MATRYOSHKA_UNSUPPORTED_MARKER in resp.text.lower()
        ):
            logger.warning(
                "embedding backend did not recognize Qwen3 MRL; requesting the native vector "
                "and reducing it to %d dimensions locally",
                _POLARIS_EMBEDDING_DIM,
            )
            resp = await self._post_with_retry(url, {"model": model, "input": texts})
        resp.raise_for_status()
        data = resp.json()["data"]
        # 按 index 还原顺序（OpenAI 兼容端点保证有 index 字段）
        data.sort(key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in data]
        if not qwen3_embedding:
            return vectors
        return [
            vector
            if len(vector) == _POLARIS_EMBEDDING_DIM
            else _truncate_and_normalize(vector, _POLARIS_EMBEDDING_DIM)
            for vector in vectors
        ]

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
