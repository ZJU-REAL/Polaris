"""Polaris MCP 服务：把统一只读工具注册表暴露给外部 MCP 客户端。

- HTTP：``app.mcp.http.router`` 挂在 FastAPI 主应用 ``POST /mcp``（见 app/main.py）。
- stdio：``python -m app.mcp``（本地桌面客户端，如 Claude Desktop）。
- 协议核心与两种传输共用 ``app.mcp.dispatch``。
"""

from app.mcp.dispatch import handle_rpc, tool_definitions
from app.mcp.http import router as mcp_router

__all__ = ["handle_rpc", "mcp_router", "tool_definitions"]
