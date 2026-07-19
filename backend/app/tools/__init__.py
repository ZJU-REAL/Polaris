"""统一只读检索工具层（docs/api-mcp.md）。

单一事实源：工具在 ``app/tools/*`` 里用 ``@tool`` 注册，既供内部 agent
（``agents/voyage/tool_loop.run_tool_loop``）动态调用，又供外部 MCP 服务器
（``app/mcp``）暴露。导入本包即触发所有工具注册。
"""

from app.tools import (
    external,  # noqa: F401 — 导入即注册工具
    knowledge,  # noqa: F401
    literature,  # noqa: F401
    project_state,  # noqa: F401
)
from app.tools.context import ToolContext
from app.tools.registry import (
    ToolSpec,
    get_tool,
    known_tools,
    list_tools,
    render_tool_specs,
    run_tool,
    tool,
)

__all__ = [
    "ToolContext",
    "ToolSpec",
    "get_tool",
    "known_tools",
    "list_tools",
    "render_tool_specs",
    "run_tool",
    "tool",
]
