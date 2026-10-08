"""登记表里的 agent → 可拉起的 AgentSpec；以及「探测」（#836）。

探测 = 真拉起一次、做完 initialize 握手就关掉。它回答的是管理面最常问的那句话：
「这个 agent 在这台机器上到底能不能用」。结果（agent 自述或失败原因）写回行上，
和外部 MCP 服务器的 last_error 同一个理由：连不上是常态，原因必须就地可见。
"""

from __future__ import annotations

import logging
import tempfile
from typing import Any

from app.models.acp_agent import AcpAgent
from app.models.base import utcnow
from app.services.acp.client import AcpClient, AgentSpec
from app.services.acp.connection import AcpError
from app.services.acp.templates import get_template, which
from app.services.mcp_hub.registry import decrypt_env

logger = logging.getLogger("polaris.acp")


def spec_from_row(row: AcpAgent) -> AgentSpec:
    policy = (
        row.permission_policy if row.permission_policy in ("deny", "read_only", "auto") else "deny"
    )
    return AgentSpec(
        name=row.name or row.slug,
        command=row.command,
        args=tuple(str(a) for a in (row.args or [])),
        env=decrypt_env(row.env_encrypted),
        permission_policy=policy,  # type: ignore[arg-type]
    )


def explain_failure(row: AcpAgent, exc: BaseException, stderr: str = "") -> str:
    """把失败翻成用户能照着做的一句话：缺命令就给安装命令，要登录就给登录命令。"""
    template = get_template(row.template)
    if isinstance(exc, AcpError) and exc.code == "spawn-failed":
        hint = f" Install it with: {template.install}" if template and template.install else ""
        return f"Command not found or not executable: {row.command}.{hint}"
    text = str(exc)
    lowered = (text + "\n" + stderr).lower()
    if any(s in lowered for s in ("auth", "login", "api key", "unauthorized", "credential")):
        hint = (
            f" Sign in on this machine first: {template.login}"
            if template and template.login
            else ""
        )
        return f"{text}{hint}"
    return text


async def probe(row: AcpAgent) -> dict[str, Any]:
    """拉起、握手、关掉；成败都写回行上，返回 {"ok", "info", "error"}。"""
    spec = spec_from_row(row)
    with tempfile.TemporaryDirectory(prefix="polaris-acp-probe-") as scratch:
        client = AcpClient(spec, root=scratch)
        try:
            info = await client.start()
        except Exception as exc:  # noqa: BLE001 — 外部进程什么都可能抛
            row.last_probe = None
            row.last_error = explain_failure(row, exc, client.stderr_tail)[:2000]
            row.last_probed_at = utcnow()
            return {"ok": False, "info": None, "error": row.last_error}
        finally:
            await client.close()
    row.last_probe = info.as_dict()
    row.last_error = None
    row.last_probed_at = utcnow()
    return {"ok": True, "info": row.last_probe, "error": None}


def command_found(row: AcpAgent) -> bool:
    return which(row.command) is not None


__all__ = ["command_found", "explain_failure", "probe", "spec_from_row"]
