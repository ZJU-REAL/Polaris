"""MCP 元信息（供前端「MCP 工具」页展示工具目录与接入信息）。

只读、非项目相关：任何登录用户都可看工具目录。MCP 协议本身在 ``POST /mcp``
（见 app/mcp/http.py、docs/api-mcp.md）；这里只返回给前端渲染用的静态目录。
"""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.auth import current_active_user
from app.mcp.dispatch import PROTOCOL_VERSION, SERVER_INFO
from app.models.user import User
from app.tools import list_tools

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _params(input_schema: dict[str, Any]) -> list[dict[str, Any]]:
    props: dict[str, Any] = input_schema.get("properties", {}) or {}
    required = set(input_schema.get("required", []) or [])
    out: list[dict[str, Any]] = []
    for name, schema in props.items():
        out.append(
            {
                "name": name,
                "required": name in required,
                "type": schema.get("type", "string"),
                "enum": schema.get("enum"),
                "description": schema.get("description"),
            }
        )
    return out


@router.get("/tools")
async def list_mcp_tools(_user: User = Depends(current_active_user)) -> dict[str, Any]:
    """MCP 只读工具目录 + 服务元信息（前端展示 + 生成接入配置用）。"""
    return {
        "server": SERVER_INFO,
        "protocol_version": PROTOCOL_VERSION,
        "endpoint": "/mcp",
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "network": spec.network,
                "read_only": spec.read_only,
                "params": _params(spec.input_schema),
            }
            for spec in list_tools()
        ],
    }
