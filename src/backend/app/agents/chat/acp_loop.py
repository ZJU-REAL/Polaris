"""助手的另一种大脑：把一轮对话交给外部 agent（ACP，#836）。

和 ChatAgentLoop 吐**同一套事件**，所以 SSE 帧、整条时间线落库、前端渲染一行不用改：

    agent_message_chunk → DeltaEvent        agent_thought_chunk → ThinkingEvent
    plan                → PlanEvent         tool_call / tool_call_update → ToolCall/ToolResult

## 工具名为什么是 agent_<kind>

ACP 的工具调用只有一个给人看的标题（「Read README.md」）和一个粗分类（read、edit、
execute…）。落库的块会在下一轮回放给模型——用户随时可以把这个对话切回 Polaris 自己
的模型——而各家 API 要求工具名是标识符，带空格的标题会被直接 400。所以名字用
``agent_<kind>``，标题放进参数里给界面显示。

## 上下文从哪来

agent 自己的会话记得之前聊过什么；进程被回收后能 ``session/load`` 就续上。都不行
（新进程、agent 不支持续会话）就把最近几轮的文字摘要拼在这轮提问前面——宁可多花
一点 token，也不让它像失忆一样从头问起。

会话还活着（或续上了）也可能有它没见过的内容：这个对话中间有几轮是 Polaris 自己答的。
上层记着 agent 见过的最后一条消息（会话设置 ``acp_seen_seq``），把那之后的消息作为
``unseen`` 传进来，同样拼成摘要补上。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.chat.events import (
    ChatEvent,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    PermissionRequestEvent,
    PermissionResolvedEvent,
    PlanEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.core.llm.base import Message
from app.services.acp.client import READ_ONLY_KINDS, AcpClient, AgentSpec
from app.services.acp.connection import AcpError
from app.services.acp.pool import AcpSessionPool

#: ACP stopReason → 我们的 stop_reason（界面据此说人话）
_STOP_REASONS = {
    "end_turn": "stop",
    "max_tokens": "length",
    "max_turn_requests": "max_rounds",
    "refusal": "refusal",
    "cancelled": "cancelled",
}
_PLAN_STATUS = {"pending": "pending", "in_progress": "running", "completed": "done"}
#: 拼进提示词的历史摘要上限（字符）
HISTORY_PREFACE_LIMIT = 12_000

logger = logging.getLogger("polaris.acp")

McpFactory = Callable[
    [AcpClient], Awaitable[tuple[list[dict[str, Any]], list[Callable[[], Awaitable[None]]]]]
]


@dataclass(slots=True)
class AcpTurnRequest:
    conversation_id: uuid.UUID
    question: str
    agent_id: uuid.UUID
    agent_name: str
    spec: AgentSpec
    workdir: str
    #: 上次这个对话在 agent 那边的会话号（用来 session/load）
    resume_session_id: str | None = None
    page_context: str = ""
    #: 最近若干轮（新进程、没能续上会话时整段补给 agent）
    history: list[Message] = field(default_factory=list)
    #: agent 那个会话没见过的消息（上次它答完之后，Polaris 自己答的那几轮）
    unseen: list[Message] = field(default_factory=list)
    #: 这一轮附带的指示：模式（计划/目标）、目标、用户让助手一直记得的事
    instructions: str = ""
    #: 路由上填的模型（agent 提供 session/set_model 时用上）
    model: str = ""


def history_preface(history: list[Message], limit: int = HISTORY_PREFACE_LIMIT) -> str:
    """最近几轮的纯文字摘要（只要用户问了什么、助手答了什么，工具细节不要）。"""
    lines: list[str] = []
    for msg in history:
        text = msg.text.strip()
        if not text or msg.role not in ("user", "assistant"):
            continue
        # 工具结果按 Anthropic 形状装在 user 消息里，是 JSON，不是人说的话
        if msg.role == "user" and text.startswith("{") and '"summary"' in text:
            continue
        who = "User" if msg.role == "user" else "Assistant"
        lines.append(f"{who}: {text}")
    body = "\n\n".join(lines)
    if len(body) > limit:
        body = "…" + body[-limit:]
    if not body:
        return ""
    return (
        "Earlier in this conversation (for context; you may not have seen it):\n\n"
        f"{body}\n\n---\n\n"
    )


class AcpChatLoop:
    def __init__(self, *, pool: AcpSessionPool, mcp_factory: McpFactory | None = None) -> None:
        self.pool = pool
        self.mcp_factory = mcp_factory
        #: 这一轮在 agent 那边用的会话号；上层落到会话设置里，下次续用
        self.session_id: str | None = None

    async def run(self, req: AcpTurnRequest) -> AsyncIterator[ChatEvent]:
        message_id = str(uuid.uuid4())
        yield MetaEvent(
            conversation_id=str(req.conversation_id),
            message_id=message_id,
            model=req.agent_name,
        )
        Path(req.workdir).mkdir(parents=True, exist_ok=True)
        key = str(req.conversation_id)
        try:
            live = self.pool.get(key)
            fresh = live is None or live.agent_id != req.agent_id
            if fresh:
                live = await self.pool.open(
                    key,
                    agent_id=req.agent_id,
                    spec=req.spec,
                    cwd=req.workdir,
                    mcp_servers=self.mcp_factory,
                    resume_session_id=req.resume_session_id,
                )
        except AcpError as exc:
            yield ErrorEvent(detail=_explain(req, exc), code="ACP_AGENT_UNAVAILABLE")
            return
        except Exception as exc:  # noqa: BLE001 — 外部进程什么都可能抛
            yield ErrorEvent(detail=f"{req.agent_name}: {exc}", code="ACP_AGENT_UNAVAILABLE")
            return
        assert live is not None
        self.session_id = live.session_id

        # 新进程且没能续上旧会话：agent 不知道前面聊过什么，补一段摘要；会话还在（或续上
        # 了）但中间有几轮是 Polaris 答的：补那几轮
        if fresh and not live.resumed:
            preface = history_preface(req.history)
        else:
            preface = history_preface(req.unseen)
        notes = (
            f"Instructions from Polaris for this turn:\n{req.instructions}\n\n---\n\n"
            if req.instructions
            else ""
        )
        prompt = (
            preface + notes + (f"{req.page_context}\n\n" if req.page_context else "") + req.question
        )

        #: tool id → (工具名, 开始时刻, 最新的标题)。标题常在后续的 tool_call_update
        #: 里才补全（Claude Code 先报 "Read File"，再补成 "Read /path/notes.txt"）
        started: dict[str, tuple[str, float, str]] = {}
        stop_reason = "stop"
        async with live.lock:
            live.last_used = time.monotonic()
            try:
                if req.model and live.model != req.model:
                    if await live.client.set_model(live.session_id, req.model):
                        live.model = req.model
                    else:
                        logger.warning(
                            "ACP agent %s 不认模型 %r，用它的默认模型（可选：%s）",
                            req.agent_name,
                            req.model,
                            ", ".join(live.client.available_models()) or "不支持选模型",
                        )
                async for upd in live.client.prompt(live.session_id, prompt):
                    kind = upd["type"]
                    if kind == "message":
                        yield DeltaEvent(text=upd["text"])
                    elif kind == "thought":
                        yield ThinkingEvent(text=upd["text"])
                    elif kind == "plan":
                        yield PlanEvent(
                            steps=tuple(
                                {
                                    "title": e["content"],
                                    "status": _PLAN_STATUS.get(e["status"], "pending"),
                                }
                                for e in upd["entries"]
                            )
                        )
                    elif kind == "tool_call":
                        name = f"agent_{upd['kind'] or 'other'}"
                        started[upd["id"]] = (name, time.monotonic(), upd["title"])
                        yield ToolCallEvent(
                            id=upd["id"],
                            name=name,
                            args={"title": upd["title"], "input": upd["input"]},
                            read_only=upd["kind"] in READ_ONLY_KINDS,
                        )
                        if upd["status"] in ("completed", "failed"):
                            res = _result(upd["id"], started, upd["status"], upd["title"], "")
                            if res is not None:
                                yield res
                    elif kind == "tool_update":
                        if upd["title"] and upd["id"] in started:
                            name, t0, _ = started[upd["id"]]
                            started[upd["id"]] = (name, t0, upd["title"])
                        if upd["status"] in ("completed", "failed"):
                            res = _result(
                                upd["id"], started, upd["status"], upd["title"], upd["output"]
                            )
                            if res is not None:
                                yield res
                    elif kind == "permission_request":
                        yield PermissionRequestEvent(
                            request_id=upd["request_id"],
                            tool_id=upd["id"],
                            title=upd["title"],
                            kind=upd["kind"],
                            input=upd["input"],
                            options=tuple(upd["options"]),
                            timeout_s=upd["timeout_s"],
                        )
                    elif kind == "permission":
                        if upd.get("request_id"):
                            yield PermissionResolvedEvent(
                                request_id=upd["request_id"], outcome=upd["outcome"]
                            )
                        if upd["outcome"] == "denied":
                            # 拒绝本身就是结果：不补一条，界面上这张工具卡会一直转圈
                            res = _result(
                                upd["id"],
                                started,
                                "failed",
                                upd["title"],
                                "Not approved."
                                if upd.get("request_id")
                                else "Blocked by this agent's permission policy.",
                            )
                            if res is not None:
                                yield res
                    elif kind == "done":
                        stop_reason = _STOP_REASONS.get(upd["stop_reason"], upd["stop_reason"])
                # agent 没给收尾的工具（它自己忘了报）：补成失败，别让卡片一直转圈
                for res in _unfinished(started):
                    yield res
            except AcpError as exc:
                # 进程死在半路：下一轮重新拉起，别留一个僵尸会话在池子里。只关**这个**
                # 会话——按 key 关的话，可能关掉另一轮刚为这个对话拉起的新会话
                await self.pool.close_if(key, live)
                # 已经开了头的工具同样补上结尾，再报错（断线/取消时这里不能再 yield，
                # 落库那边 _TurnTimeline 会给没结果的调用补一条）
                for res in _unfinished(started):
                    yield res
                yield ErrorEvent(detail=_explain(req, exc), code="ACP_AGENT_FAILED")
                return
            finally:
                live.last_used = time.monotonic()
        yield DoneEvent(stop_reason=stop_reason, message_id=message_id)


def _result(
    tool_id: str,
    started: dict[str, tuple[str, float, str]],
    status: str,
    title: str,
    output: str,
) -> ToolResultEvent | None:
    """每个工具调用只收一次尾；没见过开头的（或已经收过尾的）返回 None。"""
    if tool_id not in started:
        return None
    name, t0, last_title = started.pop(tool_id)
    ok = status != "failed"
    return ToolResultEvent(
        id=tool_id,
        name=name,
        ok=ok,
        summary=title or last_title or ("done" if ok else "failed"),
        preview=output,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


def _unfinished(started: dict[str, tuple[str, float, str]]) -> list[ToolResultEvent]:
    out = []
    for tool_id in list(started):
        res = _result(tool_id, started, "failed", "", "No result reported.")
        if res is not None:
            out.append(res)
    return out


def _explain(req: AcpTurnRequest, exc: AcpError) -> str:
    if exc.code == "spawn-failed":
        return (
            f"{req.agent_name} could not be started: {exc}. Check it in Settings → Agent backends."
        )
    return f"{req.agent_name}: {exc}"


__all__ = ["AcpChatLoop", "AcpTurnRequest", "history_preface"]
