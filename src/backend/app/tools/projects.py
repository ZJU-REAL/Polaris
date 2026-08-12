"""课题发现工具：在不知道 project_id 时列出当前用户可访问的课题。"""

from __future__ import annotations

from typing import Any

from app.core.db import get_sessionmaker
from app.services import projects as projects_service
from app.tools.context import ToolContext
from app.tools.registry import tool

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50
_STATEMENT_CHARS = 300


def _page_arg(args: dict[str, Any], name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = args.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} 必须是整数") from e
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


@tool(
    "list_accessible_projects",
    description=(
        "列出当前认证用户可访问的课题及其 project_id。"
        "调用其他 MCP 工具前不知道 project_id 时先调用本工具"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "按课题名称或 slug 模糊筛选",
            },
            "status": {
                "type": "string",
                "enum": ["active", "archived"],
                "description": "按课题状态筛选；不传时返回全部状态",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_LIMIT,
                "default": _DEFAULT_LIMIT,
                "description": "本页最多返回的课题数",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "分页起始偏移量",
            },
        },
    },
    scope="user",
    summarize=lambda a, r: f"可访问课题（{len(r.get('projects') or [])} 个）",
)
async def list_accessible_projects(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """返回当前用户可访问的课题，供后续项目级工具选择 project_id。"""
    if ctx.user_id is None:
        raise ValueError("无法确定当前用户，不能列出可访问课题")

    query = str(args.get("query") or "").strip() or None
    if query is not None and len(query) > 255:
        raise ValueError("query 最长 255 个字符")
    status = str(args.get("status") or "").strip() or None
    if status not in {None, "active", "archived"}:
        raise ValueError("status 只能是 active 或 archived")
    limit = _page_arg(args, "limit", default=_DEFAULT_LIMIT, minimum=1, maximum=_MAX_LIMIT)
    offset = _page_arg(args, "offset", default=0, minimum=0, maximum=1_000_000)

    async with get_sessionmaker()() as session:
        projects, total_count = await projects_service.list_projects_page(
            session,
            ctx.user_id,
            query=query,
            status=status,
            limit=limit,
            offset=offset,
        )

    items = [
        {
            "project_id": str(project.id),
            "name": project.name,
            "slug": project.slug,
            "statement": (project.statement or "")[:_STATEMENT_CHARS] or None,
            "status": project.status,
            "updated_at": project.updated_at.isoformat(),
        }
        for project in projects
    ]
    next_offset = offset + len(items)
    has_more = next_offset < total_count
    return {
        "projects": items,
        "total_count": total_count,
        "count": len(items),
        "offset": offset,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    }
