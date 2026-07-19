"""[兼容 shim] 文献工具集已迁移到统一注册表 ``app/tools``。

历史调用点（``actions_proposal``）通过 ``ActionContext`` 调 ``run_tool``；这里把
``ActionContext`` 适配成 ``ToolContext`` 后转发给注册表。新代码请直接用
``app.tools`` + ``app.agents.voyage.tool_loop.run_tool_loop``。
"""

from __future__ import annotations

from typing import Any

from app.agents.voyage.actions import ActionContext
from app.tools import ToolContext, render_tool_specs
from app.tools import known_tools as _known_tools
from app.tools import run_tool as _run_tool

# 想法构建原本暴露的 5 个文献工具（顺序与旧手写 TOOL_SPECS 一致）。
LIT_TOOL_NAMES = ["search_papers", "read_wiki", "read_fulltext", "get_concept", "list_concepts"]

TOOL_SPECS = render_tool_specs(LIT_TOOL_NAMES)


def tool_context_from_action(ctx: ActionContext) -> ToolContext:
    """把 Voyage 的 ActionContext 收窄成只读工具需要的 ToolContext。"""
    return ToolContext(
        project_id=ctx.run.project_id,
        llm=ctx.llm,
        user_id=ctx.run.created_by,
        voyage_id=ctx.run.id,
    )


def known_tools() -> frozenset[str]:
    return _known_tools()


async def run_tool(ctx: ActionContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return await _run_tool(tool_context_from_action(ctx), name, args)
