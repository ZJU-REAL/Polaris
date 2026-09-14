"""登记表里的 MCP 服务器 → 活的会话与工具（#754）。

把 ``McpServer`` 行翻译成 client.py 认识的 ``McpServerSpec``，并负责「同步一台
服务器的工具」这件事的事务性：连上、列工具、登记进工具注册表、把结果与错误写回
行上供管理面展示。

## 为什么错误要落库

外部软件连不上是常态（许可证没起、进程没开、端口变了）。如果失败只进日志，
管理面就只能显示「没有工具」，用户无从判断是「这台服务器没工具」还是「根本没连上」。
``last_error`` 让这两件事可区分。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.models.mcp_server import McpServer
from app.services.mcp_hub.bridge import drop_server_tools, sync_server_tools
from app.services.mcp_hub.client import McpHubError, McpServerSpec

logger = logging.getLogger("polaris.mcp_hub")


def encrypt_env(env: dict[str, str] | None) -> str | None:
    """整体加密（见 models/mcp_server 的说明）；空 env 存 NULL 而不是加密的 '{}'。"""
    if not env:
        return None
    return encrypt_secret(json.dumps(env, ensure_ascii=False))


def decrypt_env(token: str | None) -> dict[str, str]:
    """解不开就当空 env：密钥轮换过的存量行不该让整台服务器彻底不可用——
    命令还在，用户看到的是「连不上」而不是一个无法解释的崩溃。"""
    if not token:
        return {}
    try:
        data = json.loads(decrypt_secret(token))
    except Exception:  # noqa: BLE001
        logger.warning("MCP 服务器 env 解密失败，按空 env 处理", exc_info=True)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def spec_from_row(row: McpServer) -> McpServerSpec:
    return McpServerSpec(
        id=row.slug,
        transport="http" if row.transport == "http" else "stdio",
        command=row.command,
        args=tuple(str(a) for a in (row.args or [])),
        env=decrypt_env(row.env_encrypted),
        cwd=row.cwd,
        url=row.url,
    )


async def enabled_servers(session: AsyncSession) -> list[McpServer]:
    rows = await session.execute(select(McpServer).where(McpServer.enabled.is_(True)))
    return list(rows.scalars().all())


async def sync_server(session: AsyncSession, row: McpServer) -> dict[str, Any]:
    """连一台服务器并把它的工具登记进注册表；成败都写回行上。

    返回 {"ok": bool, "tools": [...], "error": str|None}——**结果是数据不是异常**：
    管理面要就地显示「这台连不上，原因是……」，而不是整个请求 500。
    """
    spec = spec_from_row(row)
    try:
        names = await sync_server_tools(spec)
    except McpHubError as exc:
        row.last_error = f"{exc.code}: {exc}"[:1000]
        row.last_tools = []
        return {"ok": False, "tools": [], "error": row.last_error}
    except Exception as exc:  # noqa: BLE001 — 外部进程什么都可能抛
        row.last_error = f"unexpected: {exc}"[:1000]
        row.last_tools = []
        return {"ok": False, "tools": [], "error": row.last_error}
    row.last_error = None
    row.last_tools = names
    return {"ok": True, "tools": names, "error": None}


async def forget_server(row: McpServer) -> None:
    """停用/删除：摘掉它的工具并断开会话。"""
    await drop_server_tools(row.slug)


async def sync_all_enabled(session: AsyncSession) -> dict[str, Any]:
    """启动/手动刷新时把所有启用的服务器同步一遍。

    一台失败不影响其余：外部软件各自独立，一台许可证过期不该让另外两台也用不了。
    """
    results: dict[str, Any] = {}
    for row in await enabled_servers(session):
        results[row.slug] = await sync_server(session, row)
    return results
