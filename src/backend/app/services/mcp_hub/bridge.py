"""把外部 MCP 服务器的工具接进 Polaris 的工具注册表。

注册表的 ``ToolSpec.input_schema`` 本来就是 JSON Schema（文件头写着「MCP 直接复用」），
所以外部工具不需要任何形状转换，接线就是改名字 + 包一层 handler。

## 命名空间

外部工具一律叫 ``mcp:<server_id>:<tool>``。理由与 LLM stage 的 ``plugin:<pack>:<stage>``
（#736）一致：
- 不同软件难免有同名工具（谁都可能叫 ``export``），不带前缀迟早撞；
- agent 的日志与审计里一眼能看出这步调用出了 Polaris，去了哪台服务器；
- 撤掉一台服务器时，按前缀就能把它的工具整批摘干净。

## 只读性

外部工具**一律按可写对待**（``read_only=False``）。Polaris 自己的检索工具是只读的，
但 CAD 建模、网格划分、跑求解器都会改外部世界的状态——把它们标成只读会让任何
"只读模式"的判断失真。宁可保守。
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.mcp_hub.client import (
    McpHub,
    McpServerSpec,
    McpToolRef,
    get_mcp_hub,
)
from app.tools.registry import ToolSpec, register_spec, unregister_prefix

logger = logging.getLogger("polaris.mcp_hub")

NAMESPACE = "mcp"


def qualified_name(server_id: str, tool: str) -> str:
    return f"{NAMESPACE}:{server_id}:{tool}"


def _handler_for(spec: McpServerSpec, ref: McpToolRef, hub: McpHub):
    async def handler(_ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        session = await hub.session(spec)
        return await session.call_tool(ref.name, args)

    return handler


def tool_spec_for(spec: McpServerSpec, ref: McpToolRef, hub: McpHub) -> ToolSpec:
    description = ref.description or f"{ref.name}（来自 MCP 服务器 {spec.id}）"
    return ToolSpec(
        name=qualified_name(spec.id, ref.name),
        description=description,
        input_schema=ref.input_schema,
        handler=_handler_for(spec, ref, hub),
        # 外部工具会改外部世界：建模、划网格、跑求解器都不是只读
        read_only=False,
        network=spec.transport == "http",
        # 外部工具不认识 Polaris 的课题模型，按登录用户隔离即可
        scope="user",
    )


async def sync_server_tools(spec: McpServerSpec, *, hub: McpHub | None = None) -> list[str]:
    """连上一台服务器，把它当前的工具重新登记一遍，返回注册后的全名列表。

    先按前缀摘干净再登记：外部服务器升级后工具可能增删改，全量替换比增量合并
    更不容易留下幽灵工具（指向已经不存在的远端工具，调用才报错）。
    """
    active = hub or get_mcp_hub()
    session = await active.session(spec)
    refs = await session.list_tools()

    unregister_prefix(f"{NAMESPACE}:{spec.id}:")
    names: list[str] = []
    for ref in refs:
        tool_spec = tool_spec_for(spec, ref, active)
        register_spec(tool_spec, replace=True)
        names.append(tool_spec.name)
    logger.info("MCP 服务器 %s 接入 %d 个工具", spec.id, len(names))
    return names


async def drop_server_tools(server_id: str, *, hub: McpHub | None = None) -> None:
    """撤掉一台服务器：工具摘干净并断开会话。"""
    unregister_prefix(f"{NAMESPACE}:{server_id}:")
    await (hub or get_mcp_hub()).close(server_id)
