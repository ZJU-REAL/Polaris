"""MCP 协议核心（传输无关）：把统一工具注册表包装成 MCP 的 tools/list + tools/call。

实现 MCP JSON-RPC 2.0 的核心方法（initialize / tools.list / tools.call / ping），
HTTP 与 stdio 两种传输共用这里的 ``handle_rpc``。默认 Profile 只读；显式授权的
Profile 可以纳入写工具。项目级工具的 inputSchema 追加必填 ``project_id`` 并在调用时
校验成员身份；用户级发现工具只依赖传输层已认证的 user_id。
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.router import get_llm_router
from app.mcp.profiles import DEFAULT_PROFILE, MCPToolProfile, profile_tools
from app.services import projects as projects_service
from app.tools import (
    ToolContext,
    get_tool,
    result_images,
    result_payload,
    run_tool,
)

# 我们支持的 MCP 协议版本（initialize 时按客户端请求回显，否则用这个）。
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "polaris", "version": "0.1.0"}

_PROJECT_ID_PROP = {
    "type": "string",
    "description": "目标项目 uuid（工具在该项目范围内检索）",
}


def _mcp_input_schema(base: dict[str, Any], *, project_scoped: bool) -> dict[str, Any]:
    """项目级工具追加 project_id；用户级工具保留原始入参。"""
    if not project_scoped:
        return dict(base)
    props = {"project_id": _PROJECT_ID_PROP, **(base.get("properties") or {})}
    required = ["project_id", *(base.get("required") or [])]
    return {**base, "type": "object", "properties": props, "required": required}


def tool_definitions(
    profile: MCPToolProfile = DEFAULT_PROFILE,
) -> list[dict[str, Any]]:
    """MCP tools/list 载荷：用户级发现工具优先，其余保持注册顺序。"""
    specs = profile_tools(profile)
    specs.sort(key=lambda spec: spec.scope != "user")
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": _mcp_input_schema(
                spec.input_schema, project_scoped=spec.scope == "project"
            ),
            "annotations": {
                "title": spec.name,
                "readOnlyHint": spec.read_only,
                "destructiveHint": False,
                "idempotentHint": spec.read_only,
                "openWorldHint": spec.network,
            },
        }
        for spec in specs
    ]


def _text_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False, default=str)
    )
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _content_blocks(result: Any) -> dict[str, Any]:
    """把工具返回转成 MCP content；只有显式携带字节的图片才内联。"""
    payload = result_payload(result)
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}
    ]
    for img in result_images(result):
        if img.data is None:
            continue
        blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(img.data).decode("ascii"),
                "mimeType": img.mime,
            }
        )
    return {"content": blocks, "isError": False}


async def call_tool(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    arguments: dict[str, Any],
    base_url: str | None = None,
    profile: MCPToolProfile = DEFAULT_PROFILE,
    allow_writes: bool = False,
) -> dict[str, Any]:
    """执行一个 Profile 允许的工具；发现与直接调用共享同一授权判断。"""
    spec = get_tool(name)
    if spec is None:
        return _text_result(f"未知工具：{name}", is_error=True)
    if not profile.exposes(spec):
        return _text_result(f"工具 {name!r} 不在 MCP Profile {profile.name!r} 中", is_error=True)
    args = dict(arguments or {})
    project_id: uuid.UUID | None = None
    if spec.scope == "project":
        raw_pid = args.pop("project_id", None)
        try:
            project_id = uuid.UUID(str(raw_pid))
        except (ValueError, TypeError):
            return _text_result(
                "缺少或非法的 project_id；请先调用 list_accessible_projects "
                "获取当前用户可访问的课题",
                is_error=True,
            )

        # 越权隔离：非项目成员一律当作项目不存在
        project = await projects_service.get_project(
            session, project_id=project_id, user_id=user_id
        )
        if project is None:
            return _text_result("项目不存在或无权访问", is_error=True)
    else:
        # 自检等内部调用方可能仍统一注入 project_id，用户级工具不向 handler 泄漏它。
        args.pop("project_id", None)

    # 默认 Profile 和普通 JWT 都保持只读。只有 Profile 明确纳入写工具，且传输层已验证
    # mcp:write scope，才把 allow_writes 打开；两道一起防止知道名字后绕过 tools/list。
    ctx = ToolContext(
        project_id=project_id,
        llm=get_llm_router(),
        user_id=user_id,
        base_url=base_url,
        allow_writes=allow_writes and profile.include_writes,
    )
    try:
        result = await run_tool(ctx, name, args)
    except PermissionError as e:
        return _text_result(str(e), is_error=True)
    except ValueError as e:
        return _text_result(str(e), is_error=True)
    return _content_blocks(result)


def _rpc_ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def handle_rpc(
    message: dict[str, Any],
    *,
    session: AsyncSession,
    user_id: uuid.UUID,
    base_url: str | None = None,
    profile: MCPToolProfile = DEFAULT_PROFILE,
    allow_writes: bool = False,
) -> dict[str, Any] | None:
    """派发单条 JSON-RPC 消息；通知（无 id / notifications.*）返回 None（无响应）。"""
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if not isinstance(method, str):
        return _rpc_err(msg_id, -32600, "Invalid Request")
    if method.startswith("notifications/") or msg_id is None:
        return None  # 通知无需响应

    if method == "initialize":
        requested = params.get("protocolVersion")
        return _rpc_ok(
            msg_id,
            {
                "protocolVersion": requested if isinstance(requested, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _rpc_ok(msg_id, {})
    if method == "tools/list":
        return _rpc_ok(msg_id, {"tools": tool_definitions(profile)})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _rpc_err(msg_id, -32602, "Invalid params: name required")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        result = await call_tool(
            session,
            user_id=user_id,
            name=name,
            arguments=arguments,
            base_url=base_url,
            profile=profile,
            allow_writes=allow_writes,
        )
        return _rpc_ok(msg_id, result)

    return _rpc_err(msg_id, -32601, f"Method not found: {method}")
