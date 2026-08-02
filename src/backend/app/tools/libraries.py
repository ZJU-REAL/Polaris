"""文献库与每日新论文池的只读工具。

课题本身不直接持有论文：语料 = 关联的方向文献库的并集。外部 harness 要判断
「这个课题的语料从哪来、够不够、上游还有什么没收进来」，就需要看得到库清单、
单库详情与上游的每日池——这三件事以前只有网页端有。

可见范围完全复用 ``libraries_service`` 的既有口径（个人库仅本人 + 全部公共库），
MCP 不放宽任何一寸。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from app.core.db import get_sessionmaker
from app.models.user import User
from app.services import daily_feed as daily_service
from app.services import libraries as libraries_service
from app.tools.context import ToolContext
from app.tools.registry import tool

_MAX_LIBRARIES = 100
_MAX_DAILY = 50
_STATEMENT_CHARS = 1200
_ABSTRACT_CHARS = 400


async def _require_user(session: Any, ctx: ToolContext) -> User:
    """库可见性判定要完整 User（角色/归属/策展人），ToolContext 只带 user_id。"""
    user = await session.get(User, ctx.user_id) if ctx.user_id is not None else None
    if user is None:
        raise ValueError("该工具需要用户身份（系统内部调用不可用）")
    return user


def _library_brief(row: dict[str, Any], *, linked: bool) -> dict[str, Any]:
    return {
        "library_id": str(row["id"]),
        "name": row["name"],
        "is_public": row["is_public"],
        "status": row["status"],
        "paper_count": row["paper_count"],
        "concept_count": row["concept_count"],
        "linked_to_this_topic": linked,
        "owner_name": row.get("owner_name"),
        "last_synced_at": row.get("last_synced_at"),
    }


@tool(
    "list_libraries",
    description="我能看到的方向文献库清单（含论文/概念计数、是否已关联到本课题）",
    input_schema={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["all", "personal", "public"],
                "description": "默认 all",
            },
            "linked_only": {
                "type": "boolean",
                "description": "只看已关联到本课题的库（即本课题的语料来源），默认 false",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIBRARIES, "default": 50},
        },
    },
    summarize=lambda a, r: f"文献库清单（{len(r.get('libraries') or [])} 个）",
)
async def list_libraries(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    want = str(args.get("type") or "all")
    linked_only = args.get("linked_only", False) is True
    limit = min(_MAX_LIBRARIES, max(1, int(args.get("limit") or 50)))
    async with get_sessionmaker()() as session:
        user = await _require_user(session, ctx)
        rows = await libraries_service.list_libraries_overview(session, user=user, type=want)
        linked_ids = set(await libraries_service.get_source_library_ids(session, ctx.project_id))
    briefs = [_library_brief(r, linked=r["id"] in linked_ids) for r in rows]
    if linked_only:
        briefs = [b for b in briefs if b["linked_to_this_topic"]]
    return {"total": len(briefs), "libraries": briefs[:limit]}


@tool(
    "get_library",
    description="文献库详情：方向说明、收录关键词与锚点论文、论文/概念计数、同步节奏",
    input_schema={
        "type": "object",
        "properties": {"library_id": {"type": "string", "description": "文献库 uuid"}},
        "required": ["library_id"],
    },
    summarize=lambda a, r: f"文献库详情：{r.get('name', '')}",
)
async def get_library(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        library_id = uuid.UUID(str(args.get("library_id")))
    except (ValueError, TypeError) as e:
        raise ValueError(f"library_id 不是合法 uuid：{args.get('library_id')}") from e
    async with get_sessionmaker()() as session:
        user = await _require_user(session, ctx)
        library = await libraries_service.get_library(session, library_id)
        # 不可见与不存在一律同一口径，不泄露存在性
        if library is None or not libraries_service.library_visible_to(library, user):
            raise ValueError(f"文献库不存在或无权访问：{args.get('library_id')}")
        row = await libraries_service.library_overview(session, library=library, user=user)
        linked_ids = set(await libraries_service.get_source_library_ids(session, ctx.project_id))
    definition = row.get("definition") or {}
    return {
        **_library_brief(row, linked=library_id in linked_ids),
        "statement": (row.get("statement") or "")[:_STATEMENT_CHARS],
        "keywords": definition.get("keywords"),
        "anchors": definition.get("anchors"),
        "cadence": row.get("cadence"),
        "monthly_budget": row.get("monthly_budget"),
        "last_compiled_at": row.get("last_compiled_at"),
        "created_at": row.get("created_at"),
    }


@tool(
    "search_daily_pool",
    description="检索每日新论文池（arXiv 当日新公告的上游候选，尚未收进任何文献库）",
    input_schema={
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "标题/摘要关键词"},
            "date": {"type": "string", "description": "某一天，格式 YYYY-MM-DD"},
            "category": {"type": "string", "description": "arXiv 分类，如 cs.CL"},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_DAILY, "default": 20},
        },
    },
    summarize=lambda a, r: f"每日池检索 → {len(r.get('papers') or [])} 篇",
)
async def search_daily_pool(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(_MAX_DAILY, max(1, int(args.get("limit") or 20)))
    raw_date = str(args.get("date") or "").strip()
    date: dt.date | None = None
    if raw_date:
        try:
            date = dt.date.fromisoformat(raw_date)
        except ValueError as e:
            raise ValueError(f"date 需要 YYYY-MM-DD 格式：{raw_date}") from e
    if ctx.user_id is None:
        raise ValueError("该工具需要用户身份（系统内部调用不可用）")

    async with get_sessionmaker()() as session:
        items, total = await daily_service.list_papers(
            session,
            user_id=ctx.user_id,
            date=date,
            q=str(args.get("q") or "").strip() or None,
            category=str(args.get("category") or "").strip() or None,
            page=1,
            size=limit,
        )
    return {
        "total": total,
        "papers": [
            {
                "paper_id": str(item["paper_id"]),
                "title": item.get("title"),
                "abstract": (item.get("abstract") or "")[:_ABSTRACT_CHARS] or None,
                "arxiv_id": item.get("arxiv_id"),
                "primary_category": item.get("primary_category"),
                "feed_date": item.get("feed_date"),
                "like_count": item.get("like_count"),
                "has_wiki": item.get("has_wiki"),
            }
            for item in items
        ],
    }
