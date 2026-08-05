"""全局助手：Claude Code 式的多轮工具循环，走 SSE。

**新路径，六个老对话端点一行不动。** 助手成熟之前两套并存，出问题随时关掉开关退回去。

事件是加法式扩展的：正文仍然走 ``delta``，形状与老端点一字不差，所以只认
``sources/delta/done/error`` 的客户端也能工作，只是看不到工具卡片。
"""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.agents.chat.loop import ChatAgentLoop, ChatTurnRequest
from app.agents.chat.prompt import DEFAULT_TOOL_NAMES
from app.api.auth import current_active_user, require_llm_chat
from app.core.config import get_settings
from app.core.db import get_session
from app.core.llm.base import TextBlock
from app.core.llm.router import get_llm_router
from app.models.user import User
from app.schemas.chat_agent import (
    ConversationCreate,
    ConversationRead,
    ConversationRenameRequest,
    ConversationTurnRequest,
    MemoryCreate,
    MessageRead,
    SkillImportRequest,
)
from app.services import agent_skills, buddy
from app.services import conversations as store
from app.services import projects as projects_service
from app.tools.context import ToolContext
from app.tools.scope import visible_library_ids

router = APIRouter(prefix="/chat", tags=["chat"])

#: 标题长度上限（与 services.conversations 自动生成时同一口径）
_TITLE_LIMIT = 60

#: 事件类 → 线上帧名。加法式：老客户端只认识其中四个，其余忽略即可。
_EVENT_NAMES: dict[type, str] = {
    MetaEvent: "meta",
    SourcesEvent: "sources",
    DeltaEvent: "delta",
    ThinkingEvent: "thinking",
    PlanEvent: "plan",
    VerifyEvent: "verify",
    ToolCallEvent: "tool_call",
    ToolResultEvent: "tool_result",
    UsageEvent: "usage",
    CompactionEvent: "compaction",
    DoneEvent: "done",
    ErrorEvent: "error",
}


def _frame(event: str, data: Any) -> str:
    # NOTE: #252 合并后改为从 app.api.chat_stream import sse_frame，这里不再自带一份
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _to_frame(ev: ChatEvent) -> str:
    name = _EVENT_NAMES.get(type(ev), "unknown")
    payload = asdict(ev) if is_dataclass(ev) else {}
    return _frame(name, payload)


def _require_enabled() -> None:
    if not get_settings().chat_agent_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CHAT_AGENT_DISABLED")


@router.post("/conversations", response_model=ConversationRead, status_code=201)
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConversationRead:
    _require_enabled()
    # 归属校验在入口做，垃圾不落库：project_id / scope_id 指向的课题必须是自己的。
    # 非成员一律 404（不区分「不存在」与「无权」，防枚举——与平台其他读口一致）。
    for pid in {payload.project_id, payload.scope_id if payload.scope_kind == "project" else None}:
        if pid is not None and await projects_service.get_project(session, pid, user.id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    conv = await store.get_or_create(
        session,
        user_id=user.id,
        scope_kind=payload.scope_kind,
        scope_id=payload.scope_id,
        project_id=payload.project_id,
    )
    await session.commit()
    return ConversationRead.model_validate(conv, from_attributes=True)


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(
    scope_kind: str | None = Query(default=None),
    scope_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ConversationRead]:
    _require_enabled()
    rows = await store.list_conversations(
        session, user_id=user.id, scope_kind=scope_kind, scope_id=scope_id, limit=limit
    )
    return [ConversationRead.model_validate(r, from_attributes=True) for r in rows]


@router.patch("/conversations/{conversation_id}", response_model=ConversationRead)
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationRenameRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConversationRead:
    """改标题。标题原本取第一句提问的前 60 字，那句话常常并不概括这场对话。"""
    _require_enabled()
    conv = await store.get_owned(session, conversation_id=conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONVERSATION_NOT_FOUND")
    conv.title = payload.title.strip()[:_TITLE_LIMIT]
    await session.commit()
    return ConversationRead.model_validate(conv, from_attributes=True)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """删掉一场对话（消息随外键级联）。只能删自己的——别人的一律 404，不是 403：
    403 等于承认「这个 id 存在」。"""
    _require_enabled()
    conv = await store.get_owned(session, conversation_id=conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONVERSATION_NOT_FOUND")
    await session.delete(conv)
    await session.commit()


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(
    conversation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[MessageRead]:
    """会话的完整消息（含工具块）。前端展开"看完整结果"时用它——SSE 里的 preview 是截断的。"""
    _require_enabled()
    from sqlalchemy import select

    from app.models.conversation import Conversation, ConversationMessage

    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONVERSATION_NOT_FOUND")
    rows = (
        (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    return [MessageRead.model_validate(r, from_attributes=True) for r in rows]


@router.get("/skills")
async def list_skills(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[dict[str, Any]]:
    """我能看到的技能：内置的 + 自己的。目录预算有限，界面上要能看到谁占着位置。"""
    _require_enabled()
    from app.models.agent_skill import as_dict

    rows = await agent_skills.visible_skills(session, user_id=user.id)
    return [as_dict(r) | {"is_builtin": r.scope == "builtin"} for r in rows]


@router.post("/skills", status_code=201)
async def import_skill(
    payload: SkillImportRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, Any]:
    """从 SKILL.md 建/更新自己的技能（同 slug 覆盖自己的那份，碰不到内置的）。"""
    _require_enabled()
    from app.models.agent_skill import as_dict
    from app.services.agent_skills import SkillParseError

    try:
        skill = await agent_skills.upsert_from_md(
            session, text=payload.skill_md, user_id=user.id, files=payload.files or {}
        )
    except SkillParseError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    await session.commit()
    return as_dict(skill)


@router.delete("/skills/{slug}", status_code=204)
async def delete_skill(
    slug: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """删自己的技能。内置技能删不掉（404）：它是代码的一部分，想改就导入同名覆盖不了、
    只能建自己的。"""
    _require_enabled()
    from sqlalchemy import select as _select

    from app.models.agent_skill import AgentSkill

    row = await session.scalar(
        _select(AgentSkill).where(AgentSkill.slug == slug, AgentSkill.owner_id == user.id)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SKILL_NOT_FOUND")
    await session.delete(row)
    await session.commit()


@router.get("/memories")
async def list_memories(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[dict[str, Any]]:
    """我让 Buddy 一直记得的事。"""
    _require_enabled()
    rows = await buddy.list_memories(session, user_id=user.id)
    return [
        {"id": str(r.id), "text": r.text, "created_at": r.created_at.isoformat()} for r in rows
    ]


@router.post("/memories", status_code=201)
async def add_memory(
    payload: MemoryCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, Any]:
    """记一条。**只有用户能写**——agent 自己往里写属于写工具那一期，
    一个能改自己记忆的助手必须和权限模型、审批一起设计。"""
    _require_enabled()
    row = await buddy.add_memory(session, user_id=user.id, text=payload.text)
    return {"id": str(row.id), "text": row.text, "created_at": row.created_at.isoformat()}


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    _require_enabled()
    if not await buddy.delete_memory(session, user_id=user.id, memory_id=memory_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="MEMORY_NOT_FOUND")


@router.get("/capabilities")
async def capabilities(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, Any]:
    """这一轮 Buddy 手里有什么：工具、技能、MCP。

    界面上要能回答「它现在到底能干什么」。此前这个问题只能靠读代码——工具白名单写在
    常量里，技能目录只在提示词里出现过，MCP 那套工具面是不是同一批也没人说得清。

    三件事在这里合并成一个答案，因为它们本来就是同一件事的三个侧面：**平台的工具面**
    （对内给 Buddy、对外给 MCP 客户端，是同一批），**技能**（按需加载的用法说明），
    以及 MCP 的暴露状况。
    """
    _require_enabled()
    from app.mcp.dispatch import tool_definitions as mcp_tool_definitions
    from app.models.agent_skill import as_dict
    from app.tools.registry import get_tool

    names = list(DEFAULT_TOOL_NAMES)
    tools = []
    for name in names:
        spec = get_tool(name)
        if spec is None:
            continue
        tools.append(
            {"name": spec.name, "description": spec.description, "read_only": spec.read_only}
        )
    skills = [
        as_dict(r) | {"is_builtin": r.scope == "builtin"}
        for r in await agent_skills.visible_skills(session, user_id=user.id)
    ]
    mcp_tools = mcp_tool_definitions()
    return {
        "tools": tools,
        "skills": [
            {
                "slug": s["slug"],
                "name": s.get("name") or s["slug"],
                "description": s.get("description") or "",
                "is_builtin": s["is_builtin"],
            }
            for s in skills
        ],
        # Polaris 自己就是 MCP 服务端：Buddy 用的工具与外部客户端（Claude Desktop 等）
        # 拿到的是同一批，只是外部那边只给只读的。如实说清楚，别让界面上多出一个
        # 并不存在的「已连接的 MCP 服务器」列表。
        "mcp": {
            "role": "server",
            "endpoint": "/mcp",
            "exposed_tools": len(mcp_tools),
            "note": "Buddy 的工具与对外 MCP 暴露的是同一批（外部客户端只拿只读工具）",
        },
    }


@router.get("/buddy/greeting")
async def buddy_greeting(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, object]:
    """PolarisBuddy 开面板时说的那句话，外加它依据的计数。

    **不过模型**：每次开面板都要出现，过 LLM 就是每次都花钱、还得等，而且模型会把
    数字说错。句子是从真实计数里挑的，不是生成的。
    """
    _require_enabled()
    stats = await buddy.collect_stats(session, user_id=user.id)
    return {
        "greeting": buddy.compose_greeting(stats, name=user.display_name or None),
        # 主动提示：没有真事就是 null，前端据此决定要不要冒气泡
        "nudge": buddy.compose_nudge(stats),
        "stats": stats.as_dict(),
    }


@router.post("/conversations/{conversation_id}/turn")
async def run_turn(
    conversation_id: uuid.UUID,
    payload: ConversationTurnRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """跑一轮：模型可以反复调工具，最后给出回答。"""
    _require_enabled()
    conv = await store.get_or_create(
        session, user_id=user.id, scope_kind="global", conversation_id=conversation_id
    )
    if conv.id != conversation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONVERSATION_NOT_FOUND")
    # 回放预算从模型路由的 context_window 推：窗口的一半给历史（字符 ≈ token×4，两个
    # 近似相抵），另一半留给工具 schema、系统提示与本轮生成。管理端没填就用保守缺省。
    budget_chars: int | None = None
    try:
        _, route = await get_llm_router().resolve("agent", user.id)
        if route.context_window:
            budget_chars = route.context_window * 2  # window/2 token × 4 字符/token
    except Exception:  # noqa: BLE001 — 路由解析失败不该挡住对话，走缺省预算
        budget_chars = None
    history = await store.replay(
        session, conversation_id=conv.id, budget_chars=budget_chars
    )
    # L1：技能目录进 system prompt（每个技能只占一行）。正文由 skill_load 按需取，
    # 绝不写进 system——那会作废整个 prompt cache 前缀。
    catalog = await agent_skills.render_catalog(session, user_id=user.id)
    # 长期记忆随 extra_system 追加在末尾（与方向说明、技能目录同处）：稳定前缀不动，
    # 缓存前缀才不会每轮作废。
    memories = await buddy.render_memories(session, user_id=user.id)
    await store.append_message(session, conversation=conv, role="user", text=payload.question)
    await session.commit()

    # PolarisBuddy 是**全局**助手：检索范围是「这个人看得见的全部文献库」，不再要求
    # 先选一个课题。课题从来不是权限边界，只是库的一个子集；要求先选课题，等于让用户
    # 替一个纯内部的数据结构做选择题，而他想问的往往正好跨库。
    #
    # 越权由可见性这一步挡住（口径与库列表页一致：管理员看全部，普通用户看自己的
    # 个人库加公共库），不是靠"只给一个课题"。
    library_ids = tuple(await visible_library_ids(session, user))
    # 本轮显式指定课题时仍然收窄到那个课题（课题里的对话还走这条路）。
    project_id = (
        await _resolve_project(session, user=user, conv=conv, requested=payload.project_id)
        if payload.project_id is not None
        else None
    )
    # 一个可见库都没有 = 没有语料可查，这轮不带工具。**不假装查过**：兜一个空范围
    # 然后说「没查到」，与从前兜随机 UUID 一样是在撒谎。
    has_corpus = bool(project_id) or bool(library_ids)
    tool_names = tuple(payload.tool_names or DEFAULT_TOOL_NAMES) if has_corpus else ()
    tool_ctx = ToolContext(
        project_id=project_id,
        # 指定了课题就按课题算范围（library_ids=None），否则给显式的可见库集合
        library_ids=None if project_id else library_ids,
        llm=get_llm_router(),
        user_id=user.id,
    )
    loop = ChatAgentLoop(llm=get_llm_router(), tool_ctx=tool_ctx, history=history)
    req = ChatTurnRequest(
        conversation_id=conv.id,
        question=payload.question,
        tool_names=tool_names,
        max_rounds=payload.max_rounds,
        statement=payload.statement,
        skill_catalog=catalog,
        extra_system=memories,
        page_context=buddy.render_page_context(payload.page_kind, payload.page_id),
    )
    conv_id, user_id = conv.id, user.id

    async def event_stream() -> AsyncIterator[str]:
        collected: list[str] = []
        stop_reason, usage = "stop", {}
        try:
            async for ev in loop.run(req):
                if isinstance(ev, DeltaEvent):
                    collected.append(ev.text)
                elif isinstance(ev, DoneEvent):
                    stop_reason, usage = ev.stop_reason, ev.usage
                yield _to_frame(ev)
        finally:
            # 断线也要落库：把已经生成的部分留住，标成 interrupted。
            # shield 是必需的——取消态下再 await 数据库写会被二次取消。
            import asyncio

            text = "".join(collected)
            if text:
                await asyncio.shield(
                    _persist_answer(conv_id, user_id, text, stop_reason, usage)
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _resolve_project(
    session: AsyncSession,
    *,
    user: User,
    conv: Any,
    requested: uuid.UUID | None,
) -> uuid.UUID | None:
    """定出这轮工具检索的课题作用域；``None`` = 不收窄到某个课题（走全局可见库）。

    只在**本轮显式指定了课题**时才走到这里——全局助手默认按可见库检索，不再有
    「先选课题」这道坎。留着它是因为课题内的对话仍然需要把范围收窄到那个课题。

    此前这里是 ``conv.project_id or conv.scope_id or uuid.uuid4()``，两个都空时兜一个
    **随机 UUID**——检索工具全按 project_id 过滤，于是助手在全局会话里永远查不到任何
    东西，还会一本正经地说「没查到」。测试没抓住它，是因为循环测试用假工具（不看
    project_id）、端点测试用不发工具调用的 fake，两层都绕开了「真工具 + 真作用域」。

    解析顺序：本轮显式指定 → 会话上已存的 → scope 指向的课题 → 用户唯一的课题
    （顺手存回会话，下轮不再解析）。每一步都做成员校验——课题成员资格是会变的，
    存过不代表现在还有权。多于一个课题且没指定时 409，让前端弹选择，**绝不替用户猜**。
    """
    scoped = conv.scope_id if conv.scope_kind == "project" else None
    for candidate in (requested, conv.project_id, scoped):
        if candidate is None:
            continue
        if await projects_service.get_project(session, candidate, user.id) is not None:
            if conv.project_id != candidate:
                conv.project_id = candidate
                await session.commit()
            return candidate
        if candidate == requested:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    mine = await projects_service.list_projects(session, user.id)
    if len(mine) == 1:
        conv.project_id = mine[0].id
        await session.commit()
        return mine[0].id
    if not mine:
        # 一个课题都没有的用户也该能纯聊天：这轮不带工具。悄悄降级成「检索不到」
        # 是此前那个随机 UUID 的做法，绝不重蹈。
        return None
    # 多于一个且没指定：409 让前端弹选择，不替用户猜
    raise HTTPException(status.HTTP_409_CONFLICT, detail="PROJECT_REQUIRED")


async def _persist_answer(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    text: str,
    stop_reason: str,
    usage: dict[str, Any],
) -> None:
    """把这一轮的回答落库。用独立 session：请求那个可能已经随连接一起没了。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        conv = await store.get_or_create(
            session, user_id=user_id, scope_kind="global", conversation_id=conversation_id
        )
        await store.append_message(
            session,
            conversation=conv,
            role="assistant",
            blocks=[TextBlock(text)],
            usage=usage or None,
            stop_reason=stop_reason,
            status="complete" if stop_reason != "interrupted" else "interrupted",
        )
        await session.commit()
