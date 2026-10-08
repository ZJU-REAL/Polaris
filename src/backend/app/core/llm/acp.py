"""外部 agent 当模型用（#840）：Polaris 的模型调用经 ACP 交给 Claude Code / Codex 等。

没配模型 API 的人多半已经在用某个编程 agent，而且登录好了。让 agent 直接回答 Polaris
的每一次模型调用，他就不必再去申请、粘贴、轮换 API key。

## 每次调用一个全新会话

路由器的调用都是一次性的（「给这篇论文打个分」「把这段改成 JSON」），上一次说过
什么与下一次无关。所以每次调用在进程里开一个新会话：没有历史串味，也不会因为
agent 记得上一篇论文而把打分带偏。会话里：

- 工作目录是这个进程专属的空目录，权限策略 ``deny``、不给 MCP——agent 能做的只有
  回答。提示词里也明说「直接作答，不要用工具」。
- system 与历史消息拍平成一段文字（ACP 没有 system 参数，也没有多轮 messages）。
- temperature / max_tokens / effort 在 ACP 里没有对应物，忽略；路由上填的模型名在
  agent 提供 ``session/set_model`` 时用上。
- 用量 agent 不报，留空由路由器按字数估算（与不报用量的中转一样）。

## 进程池

拉起一个 agent 进程要几秒（首次可能更久：npx 下载适配器），worker 里一个库同步
就能并发十几次调用。所以每个 agent 维持一小组常驻进程，用信号量限并发：拿一个
闲着的进程开会话、答完放回；进程用满若干次或闲置太久就收掉，避免它自己的会话
记录越积越多。API 与 worker 是两个进程，各有各的池。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.llm.base import (
    CompletionResult,
    EffortLevel,
    ImageBlock,
    LLMProvider,
    Message,
    StreamDone,
    StreamEvent,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolsUnsupportedError,
)

logger = logging.getLogger("polaris.acp")

#: 每个 agent 同时最多几个调用在跑（= 常驻进程上限）
DEFAULT_CONCURRENCY = int(os.environ.get("POLARIS_ACP_LLM_CONCURRENCY", "4"))
#: 一个进程答这么多次就换新的：agent 会把每个会话记在自己的存储里
MAX_USES_PER_PROCESS = 50
#: 闲置超过这么久的进程收掉
IDLE_TTL = 600.0

_DIRECT_ANSWER = (
    "You are being used as a plain language model by another program. Answer the request "
    "above directly in your reply. Do not use any tools, do not read or write files, do not "
    "run commands, and do not ask follow-up questions. Follow the requested output format "
    "exactly (for example, reply with only the JSON when JSON is asked for)."
)


@dataclass(frozen=True, slots=True)
class AcpTarget:
    """路由解析出来的那个 agent：拉起它需要的一切。frozen：它进 ResolvedRoute。"""

    agent_id: str
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    #: 配置版本（行的 updated_at）：配置改了，池子要换新进程
    version: str = ""


def render_prompt(messages: Sequence[Message], image_note: str = "") -> str:
    """system + 多轮消息 → 一段文字。最后一条之后附上「直接作答」的要求。"""
    system: list[str] = []
    turns: list[str] = []
    for msg in messages:
        text = msg.text.strip()
        if not text:
            continue
        if msg.role == "system":
            system.append(text)
        elif msg.role == "assistant":
            turns.append(f"[Assistant]\n{text}")
        else:
            turns.append(f"[User]\n{text}")
    parts: list[str] = []
    if system:
        parts.append("Instructions:\n" + "\n\n".join(system))
    if len(turns) == 1:
        parts.append(turns[0].removeprefix("[User]\n"))
    elif turns:
        parts.append("Conversation so far (answer the last User message):\n\n" + "\n\n".join(turns))
    if image_note:
        parts.append(image_note)
    parts.append(_DIRECT_ANSWER)
    return "\n\n---\n\n".join(parts)


def _collect_images(
    messages: Sequence[Message], images: list[bytes] | None
) -> list[tuple[bytes, str]]:
    out = [(data, "image/png") for data in images or []]
    for msg in messages:
        if isinstance(msg.content, list):
            out.extend((b.data, b.mime) for b in msg.content if isinstance(b, ImageBlock))
    return out


class _Worker:
    """池里的一个 agent 进程。"""

    def __init__(self, target: AcpTarget) -> None:
        from app.services.acp.client import AcpClient, AgentSpec

        self.root = tempfile.mkdtemp(prefix="polaris-acp-llm-")
        self.client = AcpClient(
            AgentSpec(
                name=target.name,
                command=target.command,
                args=target.args,
                env=dict(target.env),
                permission_policy="deny",
            ),
            root=self.root,
        )
        self.uses = 0
        self.last_used = time.monotonic()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()
        shutil.rmtree(self.root, ignore_errors=True)


class AgentProcessPool:
    """一个 agent 的常驻进程组。"""

    def __init__(self, target: AcpTarget, concurrency: int | None = None) -> None:
        self.target = target
        limit = DEFAULT_CONCURRENCY if concurrency is None else concurrency
        self._sem = asyncio.Semaphore(max(1, limit))
        self._idle: list[_Worker] = []

    @contextlib.asynccontextmanager
    async def worker(self) -> AsyncIterator[_Worker]:
        async with self._sem:
            w = await self._take()
            ok = False
            try:
                yield w
                ok = True
            finally:
                w.uses += 1
                w.last_used = time.monotonic()
                # 出过错的进程不放回：它可能停在一个半截的状态里
                if ok and w.client.alive and w.uses < MAX_USES_PER_PROCESS:
                    self._idle.append(w)
                else:
                    await w.close()

    async def _take(self) -> _Worker:
        await self.reap()
        while self._idle:
            w = self._idle.pop()
            if w.client.alive:
                return w
            await w.close()
        w = _Worker(self.target)
        try:
            await w.client.start()
        except BaseException:
            await w.close()
            raise
        return w

    async def reap(self) -> None:
        now = time.monotonic()
        keep: list[_Worker] = []
        for w in self._idle:
            if w.client.alive and now - w.last_used <= IDLE_TTL:
                keep.append(w)
            else:
                await w.close()
        self._idle = keep

    async def close(self) -> None:
        idle, self._idle = self._idle, []
        for w in idle:
            await w.close()


_pools: dict[str, AgentProcessPool] = {}


def pool_for(target: AcpTarget) -> AgentProcessPool:
    pool = _pools.get(target.agent_id)
    if pool is None or pool.target != target:
        # 配置变了（命令、参数、env）：旧池子里的进程是按旧配置拉起的
        if pool is not None:
            asyncio.ensure_future(pool.close())
        pool = AgentProcessPool(target)
        _pools[target.agent_id] = pool
    return pool


async def forget_agent(agent_id: str) -> None:
    pool = _pools.pop(agent_id, None)
    if pool is not None:
        await pool.close()


async def shutdown_pools() -> None:
    pools = list(_pools.values())
    _pools.clear()
    for pool in pools:
        await pool.close()


class AcpLLMProvider(LLMProvider):
    """把一次模型调用交给外部 agent。"""

    name = "acp"

    def __init__(self, target: AcpTarget, *, timeout: float = 300.0) -> None:
        self.target = target
        #: 单次回答的上限。agent 会自己思考一阵，比直连 API 慢，给足余量
        self.timeout = max(timeout, 180.0)

    async def _events(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        images: list[bytes] | None,
    ) -> AsyncIterator[dict[str, Any]]:
        from app.services.acp.connection import AcpError

        pool = pool_for(self.target)
        attached = _collect_images(messages, images)
        async with pool.worker() as w:
            sid = await w.client.new_session(w.root)
            if model:
                await w.client.set_model(sid, model)
            note = ""
            if attached and not w.client.info.prompt_image:
                note = (
                    f"({len(attached)} image(s) belonged to this request but this model backend "
                    "cannot view images; answer from the text alone.)"
                )
                attached = []
            prompt = render_prompt(messages, note)
            deadline = time.monotonic() + self.timeout
            gen = w.client.prompt(sid, prompt, images=attached or None)
            try:
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise AcpError("timeout", f"no answer within {self.timeout:g}s")
                    try:
                        event = await asyncio.wait_for(gen.__anext__(), timeout=left)
                    except StopAsyncIteration:
                        return
                    except TimeoutError as exc:
                        raise AcpError("timeout", f"no answer within {self.timeout:g}s") from exc
                    yield event
            finally:
                await gen.aclose()

    async def complete(
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
    ) -> CompletionResult:
        if tools:
            # 唯一传 tools 的是助手循环：它接住这个异常退回无工具作答；助手本身在
            # 解析到 agent 时直接改走 ACP 会话（带 Polaris 的 MCP 工具），见 api/chat_agent
            raise ToolsUnsupportedError("agent backends do not take Polaris tool definitions")
        text: list[str] = []
        thinking: list[str] = []
        stop = "end_turn"
        async for ev in self._events(messages, model=model, images=images):
            if ev["type"] == "message":
                text.append(ev["text"])
            elif ev["type"] == "thought":
                thinking.append(ev["text"])
            elif ev["type"] == "done":
                stop = ev["stop_reason"]
        content = "".join(text)
        return CompletionResult(
            content=content,
            model=model or self.target.name,
            finish_reason=_finish_reason(stop),
            usage={},
            blocks=(TextBlock(content),) if content else (),
        )

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
        async for ev in self._events(messages, model=model, images=images):
            if ev["type"] == "message":
                yield ev["text"]

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
        if tools:
            raise ToolsUnsupportedError("agent backends do not take Polaris tool definitions")
        stop = "end_turn"
        async for ev in self._events(messages, model=model, images=images):
            if ev["type"] == "message":
                yield TextDelta(ev["text"])
            elif ev["type"] == "thought":
                yield ThinkingDelta(ev["text"])
            elif ev["type"] == "done":
                stop = ev["stop_reason"]
        yield StreamDone(finish_reason=_finish_reason(stop))


def _finish_reason(stop: str) -> str:
    return {"end_turn": "stop", "max_tokens": "max_tokens", "cancelled": "cancelled"}.get(
        stop, stop
    )


__all__ = [
    "AcpLLMProvider",
    "AcpTarget",
    "AgentProcessPool",
    "forget_agent",
    "pool_for",
    "render_prompt",
    "shutdown_pools",
]
