"""稿件（论文）只读工具：清单、详情与文件内容。

写作阶段的产出此前对 MCP 完全不可见——外部 harness 看不到课题里有哪些稿件、
正文写到哪、上次编译过没有。本模块把这三件事补齐，只读：不建、不改、不编译。

文件按 ``path`` 寻址而不是 file_id：path 正是 ``get_manuscript`` 刚返回给模型的
东西，少一层 id 搬运就少一类张冠李戴。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.db import get_sessionmaker
from app.models.manuscript import Manuscript
from app.services import manuscripts as manuscripts_service
from app.tools.context import ToolContext
from app.tools.registry import tool

_MAX_MANUSCRIPTS = 50
_FILE_PAGE_CHARS = 6000


def _manuscript_brief(manuscript: Manuscript) -> dict[str, Any]:
    compile_info = manuscript.latest_compile or {}
    return {
        "manuscript_id": str(manuscript.id),
        "title": manuscript.title,
        "template": manuscript.template,
        "status": manuscript.status,
        "review_passed": manuscript.review_passed,
        "idea_id": str(manuscript.idea_id) if manuscript.idea_id else None,
        "experiment_id": str(manuscript.experiment_id) if manuscript.experiment_id else None,
        "last_compile": {
            "status": compile_info.get("status"),
            "at": compile_info.get("at") or compile_info.get("compiled_at"),
        }
        if compile_info
        else None,
        "updated_at": manuscript.updated_at,
    }


async def _get_manuscript(
    session: Any, ctx: ToolContext, raw_id: Any, *, with_files: bool = False
) -> Manuscript:
    try:
        manuscript_id = uuid.UUID(str(raw_id))
    except (ValueError, TypeError) as e:
        raise ValueError(f"manuscript_id 不是合法 uuid：{raw_id}") from e
    if ctx.user_id is None:
        raise ValueError("该工具需要用户身份（系统内部调用不可用）")
    manuscript = await manuscripts_service.get_manuscript_for_user(
        session, manuscript_id=manuscript_id, user_id=ctx.user_id, with_files=with_files
    )
    # 课题隔离：跨课题的稿件即便本人有权，也不该在本次 MCP 会话里露出
    if manuscript is None or manuscript.project_id != ctx.project_id:
        raise ValueError(f"本课题内不存在该稿件：{raw_id}")
    return manuscript


@tool(
    "list_manuscripts",
    description="列出本课题的稿件，含标题、模板、状态、评审结论与上次编译结果",
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_MANUSCRIPTS, "default": 20},
        },
    },
    summarize=lambda a, r: f"稿件清单（{len(r.get('manuscripts') or [])} 篇）",
)
async def list_manuscripts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(_MAX_MANUSCRIPTS, max(1, int(args.get("limit") or 20)))
    async with get_sessionmaker()() as session:
        rows = await manuscripts_service.list_manuscripts(session, project_id=ctx.project_id)
    return {
        "total": len(rows),
        "manuscripts": [_manuscript_brief(m) for m in rows[:limit]],
    }


@tool(
    "get_manuscript",
    description="取某份稿件的详情，含文件树、编译入口与引擎、上次编译诊断",
    input_schema={
        "type": "object",
        "properties": {"manuscript_id": {"type": "string", "description": "稿件 uuid"}},
        "required": ["manuscript_id"],
    },
    summarize=lambda a, r: f"稿件详情：{r.get('title', '')}",
)
async def get_manuscript(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(
            session, ctx, args.get("manuscript_id"), with_files=True
        )
        files = [
            {
                "path": f.path,
                "chars": len(f.content or ""),
                "readonly": f.readonly,
                "is_binary": f.is_binary,
                "is_folder": f.is_folder,
            }
            for f in sorted(manuscript.files, key=lambda f: f.path)
        ]
        return {
            **_manuscript_brief(manuscript),
            "main_tex": manuscript.main_tex,
            "engine": manuscript.engine,
            "files": files,
            "latest_compile": manuscript.latest_compile,
        }


@tool(
    "read_manuscript_file",
    description="读取稿件中某个文件的内容，按路径定位，长文件分页返回",
    input_schema={
        "type": "object",
        "properties": {
            "manuscript_id": {"type": "string", "description": "稿件 uuid"},
            "path": {"type": "string", "description": "文件路径，如 main.tex（见 get_manuscript）"},
            "page": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["manuscript_id", "path"],
    },
    summarize=lambda a, r: f"读稿件文件：{a.get('path', '')}",
)
async def read_manuscript_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "").strip()
    if not path:
        raise ValueError("read_manuscript_file 需要非空 path")
    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(
            session, ctx, args.get("manuscript_id"), with_files=True
        )
        target = next((f for f in manuscript.files if f.path == path), None)
        if target is None:
            available = ", ".join(sorted(f.path for f in manuscript.files)[:20])
            raise ValueError(f"稿件里没有这个文件：{path}（现有：{available}）")
        if target.is_folder:
            raise ValueError(f"{path} 是目录，不是文件")
        if target.is_binary:
            return {"path": path, "is_binary": True, "text": None, "note": "二进制文件，不返回内容"}
        content = target.content or ""

    pages = max(1, (len(content) + _FILE_PAGE_CHARS - 1) // _FILE_PAGE_CHARS)
    page = min(pages - 1, max(0, int(args.get("page") or 0)))
    start = page * _FILE_PAGE_CHARS
    return {
        "path": path,
        "page": page,
        "pages": pages,
        "readonly": target.readonly,
        "text": content[start : start + _FILE_PAGE_CHARS],
    }
