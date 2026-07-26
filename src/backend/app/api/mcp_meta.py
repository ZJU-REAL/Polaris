"""MCP 元信息与工具测试（供前端「MCP 接入」页展示目录、试运行、一键自检）。

- ``GET  /mcp/tools``：工具目录 + 接入信息（只读、非项目相关）。
- ``POST /mcp/tools/{name}/invoke``：单个工具试运行，返回 MCP 客户端会收到的原始 content。
- ``POST /mcp/selfcheck``：用课题里的真实数据把所有工具跑一遍，报告哪些已失效。

后两个都走 ``app.mcp.dispatch.call_tool``——和外部 MCP 客户端完全同一条路径
（含课题成员校验），所以页面上跑通 = 客户端也跑得通。
MCP 协议本身在 ``POST /mcp``（见 app/mcp/http.py、docs/development.md「Testing the tools」）。
"""

import asyncio
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.mcp.dispatch import PROTOCOL_VERSION, SERVER_INFO, call_tool
from app.mcp.selfcheck import TOOL_TIMEOUT, run_selfcheck
from app.models.user import User
from app.services import projects as projects_service
from app.tools import get_tool, list_tools

router = APIRouter(prefix="/mcp", tags=["mcp"])

_INVOKE_TEXT_CHARS = 20000  # 试运行的文本上限：够看清结构，又不至于撑爆页面
_INVOKE_MAX_IMAGES = 4


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
                "default": schema.get("default"),
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


class ToolInvokeRequest(BaseModel):
    """试运行入参：project_id 由服务端注入，arguments 是工具自己的参数。"""

    project_id: uuid.UUID
    arguments: dict[str, Any] = Field(default_factory=dict)


def _trim_content(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """限流返回体：长文本截断、图片最多留几张（页面只是预览）。"""
    out: list[dict[str, Any]] = []
    truncated = False
    images = 0
    for block in blocks:
        if block.get("type") == "text":
            text = str(block.get("text") or "")
            if len(text) > _INVOKE_TEXT_CHARS:
                text, truncated = text[:_INVOKE_TEXT_CHARS], True
            out.append({"type": "text", "text": text})
        elif block.get("type") == "image":
            if images >= _INVOKE_MAX_IMAGES:
                truncated = True
                continue
            images += 1
            out.append(block)
        else:
            out.append(block)
    return out, truncated


@router.post("/tools/{name}/invoke")
async def invoke_mcp_tool(
    name: str,
    body: ToolInvokeRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, Any]:
    """试运行单个工具：返回外部 MCP 客户端会收到的 content（文本 + 图片）。"""
    if get_tool(name) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未知工具：{name}")

    started = time.perf_counter()

    def _elapsed() -> int:
        return round((time.perf_counter() - started) * 1000)

    def _failure(text: str) -> dict[str, Any]:
        return {
            "name": name,
            "is_error": True,
            "duration_ms": _elapsed(),
            "content": [{"type": "text", "text": text}],
            "truncated": False,
        }

    try:
        result = await asyncio.wait_for(
            call_tool(
                session,
                user_id=user.id,
                name=name,
                arguments={**body.arguments, "project_id": str(body.project_id)},
            ),
            TOOL_TIMEOUT,
        )
    except TimeoutError:
        return _failure(f"超时（>{TOOL_TIMEOUT:.0f}s）")
    except Exception as e:  # noqa: BLE001 — 试运行要把真实异常原样回给页面
        return _failure(f"{type(e).__name__}: {e}")

    content, truncated = _trim_content(result.get("content") or [])
    return {
        "name": name,
        "is_error": bool(result.get("isError")),
        "duration_ms": _elapsed(),
        "content": content,
        "truncated": truncated,
    }


class SelfCheckRequest(BaseModel):
    """一键自检入参。"""

    project_id: uuid.UUID
    include_network: bool = False
    names: list[str] | None = None


@router.post("/selfcheck")
async def selfcheck_mcp_tools(
    body: SelfCheckRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, Any]:
    """用课题里的真实数据把每个工具跑一遍，报告哪些还能用、哪些已失效。"""
    # 报告里带课题内的样本 id，必须先过成员校验（非成员一律当作课题不存在）
    project = await projects_service.get_project(
        session, project_id=body.project_id, user_id=user.id
    )
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "课题不存在或无权访问")
    return await run_selfcheck(
        session,
        user_id=user.id,
        project_id=body.project_id,
        include_network=body.include_network,
        names=body.names,
    )
