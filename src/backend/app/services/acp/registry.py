"""登记表里的 agent → 可拉起的 AgentSpec；以及「探测」（#836）。

探测 = 真拉起一次、握手、开一个空会话就关掉。它回答的是管理面最常问的那句话：
「这个 agent 在这台机器上到底能不能用」。结果（agent 自述或失败原因）写回行上，
和外部 MCP 服务器的 last_error 同一个理由：连不上是常态，原因必须就地可见。
"""

from __future__ import annotations

import logging
import tempfile
from typing import Any, Literal

from app.core.security import decrypt_env
from app.models.acp_agent import AcpAgent
from app.models.base import utcnow
from app.services.acp.client import PERMISSION_POLICIES, AcpClient, AgentSpec
from app.services.acp.connection import AcpError
from app.services.acp.templates import get_template, which

logger = logging.getLogger("polaris.acp")


def spec_from_row(row: AcpAgent) -> AgentSpec:
    # 认不出的策略按最保守的 deny 处理（与 client 的同一份清单，加了新策略不会漏）
    policy = row.permission_policy if row.permission_policy in PERMISSION_POLICIES else "deny"
    template = get_template(row.template)
    return AgentSpec(
        name=row.name or row.slug,
        command=row.command,
        args=tuple(str(a) for a in (row.args or [])),
        env=decrypt_env(row.env_encrypted),
        permission_policy=policy,  # type: ignore[arg-type]
        passthrough=template.env_passthrough if template else (),
    )


#: 失败的几种样子；界面按它说人话、给出该做的事
FailureKind = Literal["not_installed", "needs_login", "timeout", "crashed", "failed"]

_NOT_INSTALLED = "Command not found or not executable"
_NEEDS_LOGIN = "Sign in on this machine first"
_AUTH_WORDS = (
    "auth",
    "login",
    "log in",
    "sign in",
    "api key",
    "unauthorized",
    "credential",
    "(code -32000)",
)


def failure_kind(exc: BaseException, stderr: str = "") -> FailureKind:
    """一次失败属于哪一种。ACP 里 -32000 就是「需要先登录」（auth_required）。"""
    if isinstance(exc, AcpError) and exc.code == "spawn-failed":
        return "not_installed"
    lowered = (str(exc) + "\n" + stderr).lower()
    if any(word in lowered for word in _AUTH_WORDS):
        return "needs_login"
    if isinstance(exc, AcpError) and exc.code == "timeout":
        return "timeout"
    if isinstance(exc, AcpError) and exc.code in ("not-running", "closed"):
        return "crashed"
    return "failed"


def explain_failure(row: AcpAgent, exc: BaseException, stderr: str = "") -> str:
    """把失败翻成用户能照着做的一句话：缺命令就给安装命令，要登录就给登录命令。"""
    template = get_template(row.template)
    kind = failure_kind(exc, stderr)
    if kind == "not_installed":
        hint = f" Install it with: {template.install}" if template and template.install else ""
        return f"{_NOT_INSTALLED}: {row.command}.{hint}"
    text = str(exc)
    if kind == "needs_login":
        hint = f" {_NEEDS_LOGIN}: {template.login}" if template and template.login else ""
        return f"{text}{hint}"
    return text


def error_kind(last_error: str | None) -> FailureKind | None:
    """存下来的失败原因（explain_failure 的输出）属于哪一种；没有失败返回 None。"""
    if not last_error:
        return None
    if last_error.startswith(_NOT_INSTALLED):
        return "not_installed"
    lowered = last_error.lower()
    if _NEEDS_LOGIN.lower() in lowered or any(word in lowered for word in _AUTH_WORDS):
        return "needs_login"
    if "no response within" in lowered:
        return "timeout"
    if "agent exited" in lowered or "connection closed" in lowered:
        return "crashed"
    return "failed"


async def probe(row: AcpAgent) -> dict[str, Any]:
    """拉起、握手、开一个空会话、关掉；成败都写回行上，返回 {"ok", "info", "error"}。

    只握手不够：多数智能体握手时不查登录，到开会话才说「请先登录」。开会话不发提示，
    不花额度。"""
    spec = spec_from_row(row)
    with tempfile.TemporaryDirectory(prefix="polaris-acp-probe-") as scratch:
        client = AcpClient(spec, root=scratch)
        try:
            info = await client.start()
            await client.new_session(scratch)
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


__all__ = [
    "command_found",
    "error_kind",
    "explain_failure",
    "failure_kind",
    "probe",
    "spec_from_row",
]
