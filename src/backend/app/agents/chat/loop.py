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
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents.chat.core import Helm, Navigator, Sextant
from app.agents.chat.events import (
    ChatEvent,
    CompactionEvent,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    PlanEvent,
    SourcesEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageEvent,
    VerifyEvent,
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
from app.tools.registry import get_tool

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

#: 一轮最多报多少篇论文。工具结果里可能有上百篇（scan_papers 一次 50 篇），
#: 全铺到界面上就成了第二个搜索结果页，而这一栏的用处是「刚才它看了哪几篇」。
_MAX_SOURCES = 20

#: 计划工具的名字。循环认识这一个名字，是为了在它调成功后补一帧 PlanEvent——
#: 前端不必去解析工具结果的 JSON 就能画出进度条。耦合是显式的，写在这儿。
_PLAN_TOOL = "update_plan"

#: 计划模式的出口。调成功就**收工**：这一轮到此为止，把计划交给用户点头。
#: 不这样收的话，"只调研不动手"就只是一句叮嘱——模型交完计划会顺手把活干了，
#: 而计划模式的全部价值就在于让人在花钱之前先看一眼。
_SUBMIT_PLAN_TOOL = "submit_plan"


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
    #: 这一轮已经报给前端的论文 id，避免同一篇在多次检索里重复出现
    sources_seen: set[str] = field(default_factory=set)


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
        # model 报**解析出来的真实模型**，不是 stage 名。界面上「这轮用的是谁」得是
        # 用户能对得上账单的那个名字，"agent" 不是。
        resolved_model = self._stage
        try:
            _, route = await self._llm.resolve(self._stage, self._tool_ctx.user_id)
            resolved_model = route.model or self._stage
        except Exception:  # noqa: BLE001 — 取不到就退回 stage 名，不值得为它中断一轮
            pass
        yield MetaEvent(
            conversation_id=str(req.conversation_id),
            message_id="",
            model=resolved_model,
            tools=tuple(req.tool_names),
        )

        navigator = Navigator(max_rounds=req.max_rounds, max_turn_tokens=req.max_turn_tokens)
        helm = Helm(self._tool_ctx)
        sextant = Sextant()
        answer_parts: list[str] = []
        successful_calls = 0

        while True:
            state.rounds += 1
            round_plan = navigator.plan_round(
                rounds_done=state.rounds, tokens_used=sum(state.usage.values())
            )
            tool_choice = round_plan.tool_choice

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
                        answer_parts.append(ev.text)
                        yield DeltaEvent(ev.text)
                    elif isinstance(ev, ThinkingDelta) and ev.text:
                        yield ThinkingEvent(ev.text)
                    elif isinstance(ev, StreamDone) and ev.usage:
                        for key in ("prompt_tokens", "completion_tokens"):
                            state.usage[key] = state.usage.get(key, 0) + int(ev.usage.get(key, 0))
            except ToolsUnsupportedError as e:
                # 这个中转/推理服务不认 tools。**就地降级重来一轮**，而不是把这轮判死：
                # 用户要的是答案，「你的服务端不支持函数调用」对他毫无用处。降级之后
                # 模型没有工具，只能凭上下文答——所以要明说这一轮没查库。
                logger.warning("chat agent: provider rejected tools: %s", e)
                if not specs:
                    yield ErrorEvent(str(e), code="TOOLS_UNSUPPORTED", recoverable=True)
                    return
                specs = []
                messages.append(
                    Message(
                        role="user",
                        content=(
                            "（这次运行的模型不支持调用工具，接下来只能凭已有上下文回答。"
                            "凡是需要查库才能确定的事，请明说你没查过。）"
                        ),
                    )
                )
                state.rounds -= 1  # 降级重来不算一轮，否则白扣一次预算
                continue
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
                # 验收在 done 之前：结论是这一轮的一部分，不该等下一轮才出现
                verdict = sextant.verify(
                    answer="".join(answer_parts),
                    sources=len(state.sources_seen),
                    successful_tool_calls=successful_calls,
                )
                if not verdict.passed:
                    yield VerifyEvent(passed=False, notes=verdict.notes)
                yield DoneEvent(
                    usage=dict(state.usage),
                    stop_reason=navigator.stop_reason(round_plan),
                )
                return

            messages.append(Message(role="assistant", content=list(blocks)))
            fresh_sources: list[dict[str, str]] = []
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
            awaiting_approval = False
            async for ev, result in helm.run(calls):
                if result is not None:
                    results.append(result)
                if isinstance(ev, ToolResultEvent) and ev.ok:
                    successful_calls += 1
                yield ev
                submitted = (
                    isinstance(ev, ToolResultEvent) and ev.name == _SUBMIT_PLAN_TOOL and ev.ok
                )
                if plan := _plan_from(ev, result):
                    yield PlanEvent(steps=plan, awaiting_approval=submitted)
                # 计划交出去了，这一轮就到此为止：等用户点头，别顺手把活干了。
                if submitted:
                    awaiting_approval = True
                # 「刚才它看了哪几篇」——只播**新**出现的，前端追加即可。
                # 这一栏不声称是「回答引用了这些」：我们无从核对模型的 [n] 指的是谁，
                # 说成引用就是在替它担保。
                if result is not None and ev.name != _PLAN_TOOL:
                    for ref in _paper_refs(_safe_json(result.content)):
                        if ref["paper_id"] in state.sources_seen:
                            continue
                        state.sources_seen.add(ref["paper_id"])
                        fresh_sources.append(ref)
            if fresh_sources:
                yield SourcesEvent(items=list(fresh_sources))
                fresh_sources.clear()
            messages.append(Message(role="user", content=list(results)))
            if awaiting_approval:
                # 结果照常回喂（下一轮模型看得见自己交了什么），但循环就停在这儿。
                break

            # 技能声明了 allowed-tools 就收窄后续可用的工具面。**只能收窄，永不扩权**：
            # 技能里写一个会话没给的工具名，结果是那个工具不可用，而不是把它加进来。
            if narrowed := navigator.narrow_tools(results, active_tools):
                active_tools = narrowed
                specs = tool_definitions(active_tools)

            if removed := _elide_old_results(messages):
                yield CompactionEvent(removed=removed)

def _safe_json(text: str) -> Any:
    """工具结果文本 → 对象；解析不了返回 None（结果被截断过，未必是完整 JSON）。"""
    try:
        return json.loads(text)
    except ValueError:
        return None


def _paper_refs(payload: Any) -> list[dict[str, str]]:
    """从工具结果里认出论文（paper_id + title）。

    通用遍历而不是按工具写死：search_papers 给 results、scan_papers 给 papers、
    get_paper 直接就是一篇……逐个工具适配的话，每加一个工具就得记得回来改这里，
    而漏改的表现是「引用列表少了几篇」——没人会注意到。

    只收**同时有 id 和标题**的：光有 uuid 的条目在界面上没法看，与其显示一串 uuid，
    不如不显示。
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6 or len(found) >= _MAX_SOURCES:
            return
        if isinstance(node, dict):
            pid, title = node.get("paper_id"), node.get("title")
            if (
                isinstance(pid, str)
                and isinstance(title, str)
                and title.strip()
                and pid not in seen
            ):
                seen.add(pid)
                found.append({"paper_id": pid, "title": title.strip()[:200]})
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload)
    return found


def _plan_from(
    ev: ChatEvent, result: ToolResultBlock | None
) -> tuple[dict[str, str], ...] | None:
    """成功的 update_plan / submit_plan 结果 → 计划步骤；其余一律 None。

    读的是**回喂给模型的那份文本**，不是另算一份：界面上画的计划和模型看到的计划
    必须是同一个东西，否则用户按着进度条问"第二步呢"，模型一脸茫然。
    """
    if not isinstance(ev, ToolResultEvent) or not ev.ok:
        return None
    if ev.name not in (_PLAN_TOOL, _SUBMIT_PLAN_TOOL):
        return None
    if result is None:
        return None
    try:
        payload = json.loads(result.content)
        steps = payload["steps"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(steps, list):
        return None
    return tuple(s for s in steps if isinstance(s, dict))


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
