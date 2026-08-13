"""MCP over Streamable HTTP（JSON 响应模式）：把只读工具暴露给外部 MCP 客户端。

单端点 ``POST /mcp``，JSON-RPC 2.0（支持单条与批量）。认证复用平台 JWT
（``Authorization: Bearer <token>``，即 /api/auth/jwt/login 拿到的令牌）；
项目级工具携带 project_id 并校验访问权，用户级发现工具直接使用 JWT 身份。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.mcp.dispatch import handle_rpc
from app.models.user import User

router = APIRouter(tags=["mcp"])


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
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
                    user_id=user.id,
                    base_url=base_url,
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
        user_id=user.id,
        base_url=base_url,
    )
    if resp is None:  # 通知
        return Response(status_code=202)
    return JSONResponse(resp)
