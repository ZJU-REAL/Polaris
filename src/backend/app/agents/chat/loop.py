"""对话 agent 循环。

与 ``VoyageEngine._loop()`` 是两种东西，别想着复用：那边遍历的是 Navigator 预先规划
好的 ``voyage_steps`` 数据库行、跑在 ARQ worker 里、经 Redis 对外说话、失败语义是
``paused_error`` 加重规划、取消粒度是"步"。这边没有预先计划（下一步由模型逐 token
决定）、必须在 HTTP 连接上流式输出、失败语义是"告诉用户，会话继续活着"、取消要能切
在流中途。硬套要么伪造单步计划，要么每个 token 绕一趟 Redis。

**不 import fastapi**（与 tools/ 同规矩）。循环只吐 events.py 里的事件，SSE 层负责翻译。
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents.chat.events import (
    ChatEvent,
    CompactionEvent,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageEvent,
)
from app.agents.chat.prompt import build_system_prompt, tool_definitions
from app.core.llm.base import (
    ContentBlock,
    Message,
    StreamDone,
    TextDelta,
    ThinkingDelta,
    ToolResultBlock,
    ToolsUnsupportedError,
    ToolUseBlock,
)
from app.core.llm.router import LLMRouter
from app.core.llm.tool_stream import ToolCallAccumulator
from app.tools.context import ToolContext
from app.tools.registry import get_tool, result_images, result_payload, run_tool

logger = logging.getLogger(__name__)

#: 一轮对话最多来回几次。到顶时用 tool_choice="none" 硬关工具收尾，而不是追加一条
#: user 消息哄模型 finish —— 原生 API 有确定性手段就用确定性手段。
DEFAULT_MAX_ROUNDS = 8

#: 单个工具的执行上限。网络类工具自身有 httpx 超时，但注册表层面没有统一约束。
TOOL_TIMEOUT_SECONDS = 60.0

#: 并行执行只读工具的并发度。只读才并行——写工具没有事务包裹，并行改同一行就是竞态。
_TOOL_CONCURRENCY = 4

#: 回喂给模型的单个工具结果上限。超过就截断，完整内容留在库里按需取。
RESULT_CHARS = 4000

#: 推给前端的预览上限。比回喂的更短——它只是让人看一眼"查到了什么"。
PREVIEW_CHARS = 800

#: 最近几轮的工具结果保持原样，更早的压成一行摘要。
_KEEP_FULL_RESULT_ROUNDS = 2


@dataclass(slots=True)
class ChatTurnRequest:
    conversation_id: uuid.UUID
    question: str
    tool_names: tuple[str, ...] = ()
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_turn_tokens: int | None = None
    statement: str | None = None
    extra_system: str = ""
    skill_catalog: str = ""
    #: 用户此刻在看什么（PolarisBuddy 的页面感知）。拼在本轮提问前面，**不进 system**：
    #: system 是稳定前缀，每轮变一次等于 prompt cache 永不命中。
    page_context: str = ""


@dataclass(slots=True)
class _RoundState:
    rounds: int = 0
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )
    blocks_by_round: list[tuple[ContentBlock, ...]] = field(default_factory=list)


class ChatAgentLoop:
    """一轮用户提问 → 若干次「模型思考 + 工具调用」→ 一段回答。

    每轮：组装消息 → stream_events → 转发文本/思考、喂累加器 → 流结束后落库 assistant
    消息 → 没有工具调用就收尾 → 有就并行执行、把结果作为一条 user 消息回喂 → 继续。
    """

    def __init__(
        self,
        *,
        llm: LLMRouter,
        tool_ctx: ToolContext,
        stage: str = "agent",
        history: Sequence[Message] = (),
    ) -> None:
        self._llm = llm
        self._tool_ctx = tool_ctx
        self._stage = stage
        self._history = list(history)

    async def run(self, req: ChatTurnRequest) -> AsyncIterator[ChatEvent]:
        active_tools: tuple[str, ...] = tuple(req.tool_names)
        specs = tool_definitions(active_tools)
        messages: list[Message] = [
            Message(
                role="system",
                content=build_system_prompt(
                    req.statement, req.extra_system, req.skill_catalog
                ),
            ),
            *self._history,
            Message(
                role="user",
                content=(
                    f"{req.page_context}\n\n{req.question}"
                    if req.page_context
                    else req.question
                ),
            ),
        ]
        state = _RoundState()
        yield MetaEvent(
            conversation_id=str(req.conversation_id),
            message_id="",
            model=self._stage,
            tools=tuple(req.tool_names),
        )

        while True:
            state.rounds += 1
            last_round = state.rounds >= req.max_rounds
            over_budget = (
                req.max_turn_tokens is not None
                and sum(state.usage.values()) >= req.max_turn_tokens
            )
            # 到顶就硬关工具：让模型用已经拿到的东西收尾，而不是继续查
            tool_choice = "none" if (last_round or over_budget) else None

            accumulator = ToolCallAccumulator()
            try:
                async for ev in self._llm.stream_events(
                    self._stage,
                    messages,
                    tools=specs or None,
                    tool_choice=tool_choice,
                    user_id=self._tool_ctx.user_id,
                    project_id=self._tool_ctx.project_id,
                ):
                    accumulator.feed(ev)
                    if isinstance(ev, TextDelta):
                        yield DeltaEvent(ev.text)
                    elif isinstance(ev, ThinkingDelta) and ev.text:
                        yield ThinkingEvent(ev.text)
                    elif isinstance(ev, StreamDone) and ev.usage:
                        for key in ("prompt_tokens", "completion_tokens"):
                            state.usage[key] = state.usage.get(key, 0) + int(ev.usage.get(key, 0))
            except ToolsUnsupportedError as e:
                # 这个中转不认 tools：交给调用方降级回一次性 RAG，别把这轮打成失败
                logger.warning("chat agent: provider rejected tools: %s", e)
                yield ErrorEvent(str(e), code="TOOLS_UNSUPPORTED", recoverable=True)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 转成事件，会话继续活着
                logger.warning("chat agent: stream failed", exc_info=True)
                yield ErrorEvent(f"{type(e).__name__}: {e}")
                return

            blocks = accumulator.finish()
            state.blocks_by_round.append(blocks)
            calls = [b for b in blocks if isinstance(b, ToolUseBlock)]
            if not calls:
                yield UsageEvent(
                    prompt_tokens=state.usage.get("prompt_tokens", 0),
                    completion_tokens=state.usage.get("completion_tokens", 0),
                    total_tokens=sum(state.usage.values()),
                )
                yield DoneEvent(
                    usage=dict(state.usage),
                    stop_reason=self._stop_reason(last_round, over_budget),
                )
                return

            messages.append(Message(role="assistant", content=list(blocks)))
            # 先把「要调什么」全部播出去，再开始执行：前端据此立刻画出 running 的卡片。
            # 不这么做的话，卡片会在工具跑完的那一刻凭空出现在完成态，几秒的等待期里
            # 界面上什么都没有。
            for call in calls:
                spec = get_tool(call.name)
                yield ToolCallEvent(
                    id=call.id,
                    name=call.name,
                    args=call.input,
                    read_only=spec.read_only if spec else True,
                )
            results: list[ToolResultBlock] = []
            async for ev, result in self._run_calls(calls):
                if result is not None:
                    results.append(result)
                yield ev
            messages.append(Message(role="user", content=list(results)))

            # 技能声明了 allowed-tools 就收窄后续可用的工具面。**只能收窄，永不扩权**：
            # 技能里写一个会话没给的工具名，结果是那个工具不可用，而不是把它加进来。
            if narrowed := _skill_narrowing(results, active_tools):
                active_tools = narrowed
                specs = tool_definitions(active_tools)

            if removed := _elide_old_results(messages):
                yield CompactionEvent(removed=removed)

    def _stop_reason(self, last_round: bool, over_budget: bool) -> str:
        if over_budget:
            return "turn_budget"
        if last_round:
            return "max_rounds"
        return "stop"

    async def _run_calls(
        self, calls: list[ToolUseBlock]
    ) -> AsyncIterator[tuple[ChatEvent, ToolResultBlock | None]]:
        """并行执行一轮里的工具调用，按发起顺序把事件吐出去。

        只读工具才并行。写工具（本期还没有）必须串行：没有事务包裹，并行改同一行就是
        竞态，而且审批 UI 一次弹两个框是灾难。
        """
        semaphore = asyncio.Semaphore(_TOOL_CONCURRENCY)

        async def one(call: ToolUseBlock) -> tuple[ToolResultEvent, ToolResultBlock]:
            async with semaphore:
                return await self._execute(call)

        for coro in [asyncio.create_task(one(c)) for c in calls]:
            event, block = await coro
            yield event, block

    async def _execute(self, call: ToolUseBlock) -> tuple[ToolResultEvent, ToolResultBlock]:
        started = time.monotonic()
        spec = get_tool(call.name)
        if spec is None:
            # 未知工具不打断循环：把可用清单告诉模型，它下一轮自己纠正
            payload = {"error": f"未知工具 {call.name!r}"}
            return self._failure(call, payload, started)
        if "__parse_error__" in call.input:
            payload = {"error": "参数不是合法 JSON，请重新发起这次调用"}
            return self._failure(call, payload, started)
        try:
            result = await asyncio.wait_for(
                run_tool(self._tool_ctx, call.name, call.input), timeout=TOOL_TIMEOUT_SECONDS
            )
        except TimeoutError:
            note = {"error": f"工具执行超过 {TOOL_TIMEOUT_SECONDS:.0f} 秒"}
            return self._failure(call, note, started)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 工具异常转成结果回喂，不打断循环
            logger.warning("chat agent: tool %s failed", call.name, exc_info=True)
            return self._failure(call, {"error": f"{type(e).__name__}: {e}"}, started)

        payload = result_payload(result)
        images = result_images(result)
        text = json.dumps(payload, ensure_ascii=False)[:RESULT_CHARS]
        duration = int((time.monotonic() - started) * 1000)
        summary = spec.summary(call.input, payload) if spec else call.name
        return (
            ToolResultEvent(
                id=call.id,
                name=call.name,
                ok=True,
                summary=summary,
                preview=text[:PREVIEW_CHARS],
                duration_ms=duration,
                images=len(images),
            ),
            ToolResultBlock(call.id, text),
        )

    def _failure(
        self, call: ToolUseBlock, payload: dict[str, Any], started: float
    ) -> tuple[ToolResultEvent, ToolResultBlock]:
        text = json.dumps(payload, ensure_ascii=False)
        duration = int((time.monotonic() - started) * 1000)
        return (
            ToolResultEvent(
                id=call.id,
                name=call.name,
                ok=False,
                summary=payload.get("error", "失败"),
                preview=text[:PREVIEW_CHARS],
                duration_ms=duration,
            ),
            ToolResultBlock(call.id, text, is_error=True),
        )


def _skill_narrowing(
    results: list[ToolResultBlock], current: tuple[str, ...]
) -> tuple[str, ...] | None:
    """本轮加载的技能若声明了 allowed-tools，就据此收窄。

    收窄发生在技能加载**之后**的那一轮起效——这一轮已经调过的工具不受影响，那是既成
    事实。收窄结果为空时不生效（技能写错名字不该把助手变成哑巴）。
    """
    from app.services.agent_skills import narrow_tools

    narrowed = current
    for block in results:
        if block.is_error:
            continue
        try:
            payload = json.loads(block.content)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict) or "allowed_tools" not in payload:
            continue
        narrowed = narrow_tools(narrowed, payload.get("allowed_tools"))
    if narrowed == current or not narrowed:
        return None
    return narrowed


def _elide_old_results(messages: list[Message]) -> int:
    """把较早的工具结果压成一行，只保留最近几轮的原文。

    工具结果是上下文里的大头（一次 read_fulltext 就 4000 字符）。先做这一步驱逐，往往
    就够了；LLM 摘要压缩是后备手段，成本高得多。
    """
    rounds_seen = 0
    removed = 0
    for msg in reversed(messages):
        blocks = msg.content
        if not isinstance(blocks, list):
            continue
        results = [b for b in blocks if isinstance(b, ToolResultBlock)]
        if not results:
            continue
        rounds_seen += 1
        if rounds_seen <= _KEEP_FULL_RESULT_ROUNDS:
            continue
        replaced: list[ContentBlock] = []
        for b in blocks:
            if isinstance(b, ToolResultBlock) and len(b.content) > PREVIEW_CHARS:
                replaced.append(
                    ToolResultBlock(
                        b.tool_use_id,
                        json.dumps(
                            {"elided": True, "preview": b.content[:200]}, ensure_ascii=False
                        ),
                        is_error=b.is_error,
                    )
                )
                removed += 1
            else:
                replaced.append(b)
        msg.content = replaced
    return removed


__all__ = ["DEFAULT_MAX_ROUNDS", "ChatAgentLoop", "ChatTurnRequest"]
