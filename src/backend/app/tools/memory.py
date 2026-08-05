"""Buddy 的记忆工具：自己记、自己找。

这是本期唯一**会写数据**的一对工具，但写的只是它与用户之间的记忆——不碰平台里的
论文、想法、实验。所以它不走写工具的审批面，而是走一个**用户可以关掉的开关**：
记忆没打开时这两个工具根本不进工具面，模型不会知道有这回事，也就不会假装记住。

分两层存：
- ``fact``：抽出来的稳定事实（研究方向、偏好、约定），每轮都随提示词带上；
- ``note``：带时间戳的对话片段，只在检索到时才回到上下文里。

区分这两层是因为它们的成本不同：事实每轮都要付钱，笔记只在用得上时付。把对话历史
一股脑当事实存，几十条之后每轮都在为陈年琐事付费。
"""

import uuid
from typing import Any

from app.core.db import get_sessionmaker
from app.tools.context import ToolContext
from app.tools.registry import tool

#: 记忆工具的名字。开关关掉时它们不进工具面。
MEMORY_TOOL_NAMES = ("remember", "recall")

_KINDS = ("fact", "note")


@tool(
    "remember",
    description=(
        "把值得长期记住的事写进记忆。用户说明自己的方向/偏好/约定，或本轮出现了"
        "以后还用得上的结论时调用。kind=fact 是每轮都会带上的稳定事实，"
        "kind=note 是带时间戳的片段、只在检索到时才回到上下文。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要记住的一句话"},
            "kind": {"type": "string", "enum": list(_KINDS), "default": "fact"},
        },
        "required": ["text"],
    },
    read_only=False,
    summarize=lambda a, r: f"记住（{a.get('kind', 'fact')}）：{str(a.get('text', ''))[:40]}",
)
async def remember(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services import buddy

    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("remember 需要非空 text")
    if ctx.user_id is None:
        raise ValueError("记忆是按人存的，这次调用没有用户身份")
    kind = str(args.get("kind") or "fact")
    if kind not in _KINDS:
        kind = "fact"
    async with get_sessionmaker()() as session:
        row = await buddy.add_memory(session, user_id=ctx.user_id, text=text, kind=kind)
    return {"id": str(row.id), "text": row.text, "kind": kind}


@tool(
    "recall",
    description=(
        "在长期记忆里检索。问到「我之前说过什么」「上次那个结论」，"
        "或需要回忆用户的偏好与既往约定时调用。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词；留空则返回最近的记忆"},
            "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
    },
    summarize=lambda a, r: f"回忆「{a.get('query', '')}」→ {len(r.get('items') or [])} 条",
)
async def recall(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services import buddy

    if ctx.user_id is None:
        raise ValueError("记忆是按人存的，这次调用没有用户身份")
    query = str(args.get("query") or "").strip()
    k = min(20, max(1, int(args.get("k") or 8)))
    async with get_sessionmaker()() as session:
        rows = await buddy.search_memories(session, user_id=ctx.user_id, query=query, limit=k)
    return {
        "items": [
            {
                "text": r.text,
                "kind": r.kind,
                "at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


def _unused(_: uuid.UUID) -> None:  # pragma: no cover - 保持 uuid 导入被使用
    return None
