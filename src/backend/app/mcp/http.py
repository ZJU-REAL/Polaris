"""MCP over Streamable HTTP（JSON 响应模式）：向外部 MCP 客户端暴露工具。

单端点 ``POST /mcp``，JSON-RPC 2.0（支持单条与批量）。认证支持平台 JWT 与
可吊销的 Integration Token；
项目级工具携带 project_id 并校验访问权，用户级发现工具直接使用 JWT 身份。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.integration_auth import IntegrationPrincipal, require_mcp_read
from app.core.db import get_session
from app.integrations import resolve_mcp_profile
from app.mcp.dispatch import handle_rpc
from app.mcp.profiles import MCPToolProfile
from app.services import buddy
from app.tools.memory import MEMORY_TOOL_NAMES

router = APIRouter(tags=["mcp"])


def _apply_memory_gate(profile: MCPToolProfile, principal: IntegrationPrincipal) -> MCPToolProfile:
    """Hide the memory tools unless the user turned memory on.

    Buddy memory is an opt-in that defaults off; the in-app tool surface omits
    ``remember``/``recall`` entirely until then. Mirror that here so an MCP
    write token cannot persist memory the user never enabled — the exclusion
    covers both discovery and invocation, since both consult ``profile.exposes``.
    """
    if buddy.memory_enabled(principal.user):
        return profile
    return replace(profile, excluded=profile.excluded | frozenset(MEMORY_TOOL_NAMES))


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    tool_profile: str | None = Header(default=None, alias="X-Polaris-Tool-Profile"),
    session: AsyncSession = Depends(get_session),
    principal: IntegrationPrincipal = Depends(require_mcp_read),
) -> Response:
    try:
        profile = resolve_mcp_profile(tool_profile)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    profile = _apply_memory_gate(profile, principal)
    allow_writes = "mcp:write" in principal.scopes and not principal.user.read_only
    if profile.include_writes and not allow_writes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="MCP_WRITE_SCOPE_REQUIRED")

    body: Any = await request.json()
    # 图片下载与 MCP 严格同源：客户端调的是 https://host/mcp，工具就返回
    # https://host/api/...。不读额外配置，避免 MCP 地址与下载地址发生漂移。
    base_url = str(request.base_url)

    if isinstance(body, list):  # JSON-RPC 批量
        responses = [
            resp
            for msg in body
            if isinstance(msg, dict)
            and (
                resp := await handle_rpc(
                    msg,
                    session=session,
                    user_id=principal.user.id,
                    base_url=base_url,
                    profile=profile,
                    allow_writes=allow_writes,
                )
            )
            is not None
        ]
        if not responses:  # 全是通知 → 无内容
            return Response(status_code=202)
        return JSONResponse(responses)

    if not isinstance(body, dict):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        )

    resp = await handle_rpc(
        body,
        session=session,
        user_id=principal.user.id,
        base_url=base_url,
        profile=profile,
        allow_writes=allow_writes,
    )
    if resp is None:  # 通知
        return Response(status_code=202)
    return JSONResponse(resp)
