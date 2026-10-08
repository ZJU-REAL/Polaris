"""ACP client：握手、会话、一轮对话、以及 agent 反向发来的请求（#836）。

上层（助手对话、探测接口）只和这里打交道，拿到的是**归一化**后的更新：

    {"type": "message",   "text": ...}                    正文增量
    {"type": "thought",   "text": ...}                    思考增量
    {"type": "tool_call", "id", "title", "kind", "status", "input"}
    {"type": "tool_update", "id", "status", "title", "output"}
    {"type": "plan",      "entries": [{"content", "status", "priority"}]}
    {"type": "permission", "id", "title", "kind", "outcome"}   我们替用户做的权限决定
    {"type": "done",      "stop_reason": ...}             一轮结束（总是最后一条）

## 权限策略

agent 想执行写文件、跑命令这类动作前会发 ``session/request_permission``。我们没有
人在线上点按钮（交互式审批是后续工作），所以由登记时选的策略直接回答：

- ``deny``（默认）：一律拒绝。agent 只能读、只能说。
- ``read_only``：只放行读取/搜索/抓取/思考类动作（ACP 的 tool kind：read、search、
  fetch、think），其余拒绝。
- ``auto``：每次都「允许这一次」——不选「总是允许」，让每个动作仍然逐条经过这里。
- ``ask``（#838，新登记的 agent 默认用它）：把请求原样摆到用户面前，等他点。没人点
  （超时、连接断了、这一轮被取消）就按拒绝处理——无人值守时和 ``deny`` 一样安全。

默认是 deny 而不是 auto，理由和 Agentero 一样：登记一个 agent 不等于同意它在服务器
上随便改东西。

## 文件读写只在工作目录里

我们声明自己能代读代写文件（``fs.readTextFile`` / ``fs.writeTextFile``），这样支持它
的 agent 会把文件操作交给我们，而我们把路径钉死在会话的工作目录里：解析成真实路径
之后必须仍在根目录下，``..``、符号链接都绕不出去。写文件还要求策略允许改动
（``auto``，或 ``ask`` 下用户刚批准过一次改动）。Agentero 不做这层校验；我们跑在
服务器上，必须做。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.services.acp.connection import AcpConnection, AcpError, RpcMethodError

logger = logging.getLogger("polaris.acp")

PROTOCOL_VERSION = 1
#: 首次握手可能要等 npx 下载适配器，给足时间
INITIALIZE_TIMEOUT = 90.0
SESSION_TIMEOUT = 60.0
#: 工具输入/输出进事件前截断，免得一次读大文件把 SSE 和库一起灌爆
PREVIEW_LIMIT = 4000

PermissionPolicy = Literal["deny", "read_only", "ask", "auto"]
PERMISSION_POLICIES: tuple[str, ...] = ("deny", "read_only", "ask", "auto")
READ_ONLY_KINDS = frozenset({"read", "search", "fetch", "think"})
#: 会改东西的工具类别：ask 策略下用户批准了它们，才放行随后的 fs/write_text_file
WRITE_KINDS = frozenset({"edit", "delete", "move"})
#: ask 策略下等用户回答的上限；超时按拒绝
ASK_TIMEOUT = 300.0


@dataclass(slots=True)
class AgentSpec:
    """拉起一个 agent 需要的全部信息。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    permission_policy: PermissionPolicy = "deny"


@dataclass(slots=True)
class AgentInfo:
    """握手拿到的 agent 自述。"""

    protocol_version: int | None = None
    name: str = ""
    title: str = ""
    version: str = ""
    load_session: bool = False
    mcp_http: bool = False
    mcp_sse: bool = False
    prompt_image: bool = False
    auth_methods: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "load_session": self.load_session,
            "mcp_http": self.mcp_http,
            "mcp_sse": self.mcp_sse,
            "prompt_image": self.prompt_image,
            "auth_methods": self.auth_methods,
        }


def _clip(text: str, limit: int = PREVIEW_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"… ({len(text) - limit} more chars)"


def _content_text(content: Any) -> str:
    """把 ACP 的 ContentBlock（或它的列表）压成文字；图片/音频只留占位。"""
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(_content_text(c) for c in content)
    if not isinstance(content, dict):
        return str(content)
    kind = content.get("type")
    if kind == "text":
        return str(content.get("text") or "")
    if kind in ("image", "audio"):
        return f"[{kind}]"
    if kind == "resource_link":
        return str(content.get("title") or content.get("name") or content.get("uri") or "")
    if kind == "resource":
        res = content.get("resource") or {}
        return str(res.get("text") or res.get("uri") or "")
    # tool_call 的 content 项：{"type":"content","content":{...}} / diff / terminal
    if kind == "content":
        return _content_text(content.get("content"))
    if kind == "diff":
        return f"[diff] {content.get('path', '')}"
    if kind == "terminal":
        return f"[terminal {content.get('terminalId', '')}]"
    return ""


def _jsonish(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def normalize_update(update: dict[str, Any]) -> dict[str, Any] | None:
    """ACP ``session/update`` 的 update 体 → 归一化事件；不关心的种类返回 None。"""
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        text = _content_text(update.get("content"))
        return {"type": "message", "text": text} if text else None
    if kind == "agent_thought_chunk":
        text = _content_text(update.get("content"))
        return {"type": "thought", "text": text} if text else None
    if kind == "tool_call":
        return {
            "type": "tool_call",
            "id": str(update.get("toolCallId") or ""),
            "title": str(update.get("title") or ""),
            "kind": str(update.get("kind") or "other"),
            "status": str(update.get("status") or "pending"),
            "input": _clip(_jsonish(update.get("rawInput"))),
        }
    if kind == "tool_call_update":
        output = _content_text(update.get("content")) or _jsonish(update.get("rawOutput"))
        return {
            "type": "tool_update",
            "id": str(update.get("toolCallId") or ""),
            "status": str(update.get("status") or ""),
            "title": str(update.get("title") or ""),
            "output": _clip(output),
        }
    if kind == "plan":
        entries = [
            {
                "content": str(e.get("content") or ""),
                "status": str(e.get("status") or "pending"),
                "priority": str(e.get("priority") or "medium"),
            }
            for e in (update.get("entries") or [])
            if isinstance(e, dict)
        ]
        return {"type": "plan", "entries": entries}
    return None


def decide_permission(policy: str, tool_kind: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    """按策略回答一次权限请求，返回 ACP 的 ``outcome`` 对象。"""
    allow = policy == "auto" or (policy == "read_only" and tool_kind in READ_ONLY_KINDS)
    wanted = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for want in wanted:
        for opt in options:
            if opt.get("kind") == want and opt.get("optionId"):
                return {"outcome": "selected", "optionId": opt["optionId"]}
    # 没有合适选项：拒绝时回 cancelled（协议里这就是「没同意」），允许时也只能 cancelled
    return {"outcome": "cancelled"}


def contained_path(root: str, path: str) -> Path:
    """把 agent 给的路径钉在 root 里；越界抛 RpcMethodError。"""
    if not path or not os.path.isabs(path):
        raise RpcMethodError(-32602, "path must be absolute")
    base = Path(root).resolve()
    target = Path(path).resolve()
    if target != base and base not in target.parents:
        raise RpcMethodError(-32000, "path is outside the session working directory")
    return target


class AcpClient:
    """一个 agent 进程上的 ACP client。一个进程可以开多个会话，但我们通常一个会话一个进程。"""

    def __init__(self, spec: AgentSpec, *, root: str) -> None:
        self.spec = spec
        self.root = root
        self.info = AgentInfo()
        self._sinks: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        #: ask 策略下等用户回答的请求：request_id → (会话号, future, 允许的选项 id)
        self._asks: dict[str, tuple[str, asyncio.Future[dict[str, Any]], set[str]]] = {}
        #: 用户批准过的改动：会话号 → 还能写几次；「总是允许」记进 _always_write
        self._write_grants: dict[str, int] = {}
        self._always_write: set[str] = set()
        env = {**inherited_env(), **spec.env}
        env["PATH"] = augmented_path(env.get("PATH", ""))
        self._conn = AcpConnection(
            spec.command,
            spec.args,
            env=env,
            cwd=root,
            on_notification=self._on_notification,
            on_request=self._on_request,
        )

    @property
    def alive(self) -> bool:
        return self._conn.alive

    @property
    def stderr_tail(self) -> str:
        return self._conn.stderr_tail

    # ---------------------------------------------------------------- 握手 / 会话

    async def start(self) -> AgentInfo:
        await self._conn.start()
        result = await self._conn.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False,
                },
                "clientInfo": {"name": "polaris", "title": "Polaris", "version": "1"},
            },
            timeout=INITIALIZE_TIMEOUT,
        )
        self.info = parse_initialize(result or {})
        return self.info

    async def new_session(self, cwd: str, mcp_servers: list[dict[str, Any]] | None = None) -> str:
        result = await self._conn.request(
            "session/new",
            {"cwd": cwd, "mcpServers": list(mcp_servers or [])},
            timeout=SESSION_TIMEOUT,
        )
        session_id = (result or {}).get("sessionId")
        if not session_id:
            raise AcpError("bad-response", "session/new returned no sessionId")
        return str(session_id)

    async def load_session(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> None:
        """续上一个旧会话。agent 会把历史作为 session/update 回放——那些不是这一轮的内容，
        这里不设接收队列，回放自然被丢掉。"""
        await self._conn.request(
            "session/load",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": list(mcp_servers or [])},
            timeout=SESSION_TIMEOUT,
        )

    async def prompt(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
        """发一轮提示，边跑边吐归一化事件；最后一条一定是 ``done``。"""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sinks[session_id] = queue
        request = asyncio.create_task(
            self._conn.request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=None,
            )
        )
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({getter, request}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                # 回应到了：先把已经排队的更新吐完，顺序才对
                while not queue.empty():
                    yield queue.get_nowait()
                result = request.result()
                yield {
                    "type": "done",
                    "stop_reason": str((result or {}).get("stopReason") or "end_turn"),
                }
                return
        finally:
            self._sinks.pop(session_id, None)
            # 这一轮结束了还没回答的权限请求：一律按取消回给 agent（协议要求取消时如此）
            self._settle_asks(session_id, {"outcome": "cancelled"})
            if not request.done():
                # 调用方中途不要了（断线、取消）：告诉 agent 停下，别在后台继续烧钱
                with contextlib.suppress(Exception):
                    await self._conn.notify("session/cancel", {"sessionId": session_id})
                request.cancel()
                with contextlib.suppress(BaseException):
                    await request

    async def cancel(self, session_id: str) -> None:
        self._settle_asks(session_id, {"outcome": "cancelled"})
        await self._conn.notify("session/cancel", {"sessionId": session_id})

    def answer_permission(self, request_id: str, option_id: str) -> bool:
        """用户对一次 ask 的回答。请求不存在/已答过/选项不是 agent 给的 → False。"""
        pending = self._asks.get(request_id)
        if pending is None:
            return False
        _session_id, fut, options = pending
        if fut.done() or option_id not in options:
            return False
        fut.set_result({"outcome": "selected", "optionId": option_id})
        return True

    def pending_asks(self, session_id: str) -> list[str]:
        return [
            rid for rid, (sid, fut, _) in self._asks.items() if sid == session_id and not fut.done()
        ]

    def _settle_asks(self, session_id: str, outcome: dict[str, Any]) -> None:
        for _rid, (sid, fut, _) in list(self._asks.items()):
            if sid == session_id and not fut.done():
                fut.set_result(outcome)

    async def close(self) -> None:
        await self._conn.close()

    # ---------------------------------------------------------------- 入站

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        sink = self._sinks.get(str(params.get("sessionId") or ""))
        update = params.get("update")
        if sink is None or not isinstance(update, dict):
            return
        event = normalize_update(update)
        if event is not None:
            sink.put_nowait(event)

    async def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "session/request_permission":
            tool = params.get("toolCall") or {}
            kind = str(tool.get("kind") or "other")
            session_id = str(params.get("sessionId") or "")
            options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
            sink = self._sinks.get(session_id)
            request_id = ""
            if self.spec.permission_policy == "ask":
                outcome, request_id = await self._ask_user(session_id, tool, kind, options, sink)
            else:
                outcome = decide_permission(self.spec.permission_policy, kind, options)
            chosen = _option_kind(params, outcome.get("optionId"))
            allowed = outcome.get("outcome") == "selected" and chosen.startswith("allow")
            if allowed and kind in WRITE_KINDS:
                if chosen == "allow_always":
                    self._always_write.add(session_id)
                else:
                    self._write_grants[session_id] = self._write_grants.get(session_id, 0) + 1
            if sink is not None:
                sink.put_nowait(
                    {
                        "type": "permission",
                        "id": str(tool.get("toolCallId") or ""),
                        "title": str(tool.get("title") or ""),
                        "kind": kind,
                        "outcome": "allowed" if allowed else "denied",
                        # ask 时带上请求号：界面据此把那张待批准的卡片收起来（含超时）
                        "request_id": request_id,
                    }
                )
            return {"outcome": outcome}
        if method == "fs/read_text_file":
            target = contained_path(self.root, str(params.get("path") or ""))
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError as exc:
                raise RpcMethodError(-32002, f"file not found: {params.get('path')}") from exc
            except IsADirectoryError as exc:
                raise RpcMethodError(-32602, "path is a directory") from exc
            line, limit = params.get("line"), params.get("limit")
            if line is not None or limit is not None:
                lines = text.splitlines(keepends=True)
                start = max(int(line or 1) - 1, 0)
                end = start + int(limit) if limit is not None else None
                text = "".join(lines[start:end])
            return {"content": text}
        if method == "fs/write_text_file":
            if not self._may_write(str(params.get("sessionId") or "")):
                raise RpcMethodError(-32000, "writing files is not permitted for this agent")
            target = contained_path(self.root, str(params.get("path") or ""))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(params.get("content") or ""), encoding="utf-8")
            return None
        raise RpcMethodError(-32601, f"method not supported: {method}")

    def _may_write(self, session_id: str) -> bool:
        if self.spec.permission_policy == "auto":
            return True
        if self.spec.permission_policy != "ask":
            return False
        if session_id in self._always_write:
            return True
        # 一次批准换一次写：agent 拿着一次「允许」不能顺手把整个目录都改了
        left = self._write_grants.get(session_id, 0)
        if left <= 0:
            return False
        self._write_grants[session_id] = left - 1
        return True

    async def _ask_user(
        self,
        session_id: str,
        tool: dict[str, Any],
        kind: str,
        options: list[dict[str, Any]],
        sink: asyncio.Queue[dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], str]:
        """把权限请求摆给用户并等他回答，返回 (outcome, 请求号)。

        没人接（不在一轮里、超时）就拒绝；这一轮被取消则回 cancelled——协议要求如此，
        而不是假装用户点了拒绝。"""
        refuse = decide_permission("deny", kind, options)
        if sink is None:
            return refuse, ""
        import uuid

        request_id = uuid.uuid4().hex
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._asks[request_id] = (session_id, fut, {str(o.get("optionId")) for o in options})
        sink.put_nowait(
            {
                "type": "permission_request",
                "request_id": request_id,
                "id": str(tool.get("toolCallId") or ""),
                "title": str(tool.get("title") or ""),
                "kind": kind,
                "input": _clip(_jsonish(tool.get("rawInput")), 1500),
                "options": [
                    {
                        "id": str(o.get("optionId") or ""),
                        "name": str(o.get("name") or ""),
                        "kind": str(o.get("kind") or ""),
                    }
                    for o in options
                    if o.get("optionId")
                ],
                "timeout_s": int(ASK_TIMEOUT),
            }
        )
        try:
            outcome = await asyncio.wait_for(asyncio.shield(fut), timeout=ASK_TIMEOUT)
        except TimeoutError:
            outcome = refuse
        finally:
            self._asks.pop(request_id, None)
        return outcome, request_id


def _option_kind(params: dict[str, Any], option_id: Any) -> str:
    for opt in params.get("options") or []:
        if opt.get("optionId") == option_id:
            return str(opt.get("kind") or "")
    return ""


def parse_initialize(result: dict[str, Any]) -> AgentInfo:
    caps = result.get("agentCapabilities") or {}
    mcp = caps.get("mcpCapabilities") or {}
    prompt = caps.get("promptCapabilities") or {}
    agent = result.get("agentInfo") or {}
    version = result.get("protocolVersion")
    return AgentInfo(
        protocol_version=int(version) if isinstance(version, int) else None,
        name=str(agent.get("name") or ""),
        title=str(agent.get("title") or ""),
        version=str(agent.get("version") or ""),
        load_session=bool(caps.get("loadSession")),
        mcp_http=bool(mcp.get("http")),
        mcp_sse=bool(mcp.get("sse")),
        prompt_image=bool(prompt.get("image")),
        auth_methods=[m for m in (result.get("authMethods") or []) if isinstance(m, dict)],
    )


#: agent 从服务端进程继承的环境变量：只放行这些名字/前缀。
#:
#: 服务端进程的环境里有数据库连接串、加密密钥、各家模型的 API key。agent 能跑命令
#: （它自己的工具），整份继承等于把这些交给它。白名单里只留它干活需要的：找命令、
#: 找自己的登录凭据（HOME）、语言区域、代理。agent 自己要的 key 在登记时显式配进 env。
_ENV_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TERM",
        "TMPDIR",
        "TZ",
        "SystemRoot",
        "COMSPEC",
        "PATHEXT",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)
_ENV_ALLOW_PREFIXES = ("LC_", "XDG_", "NODE_", "NPM_CONFIG_", "NVM_")
_ENV_ALLOW_LOWER = frozenset({"http_proxy", "https_proxy", "no_proxy", "all_proxy"})


def inherited_env(source: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if source is None else source
    out: dict[str, str] = {}
    for key, value in src.items():
        if (
            key in _ENV_ALLOW
            or key.lower() in _ENV_ALLOW_LOWER
            or key.upper().startswith(_ENV_ALLOW_PREFIXES)
        ):
            out[key] = value
    return out


#: 从图形界面启动（桌面端从 Finder 打开）时 PATH 很短，npm/Homebrew 装的 CLI 找不到。
#: 把常见安装位置补在末尾——只补不改，用户自己的 PATH 仍然优先。
_EXTRA_PATH_DIRS = (
    "~/.local/bin",
    "~/.npm-global/bin",
    "~/.bun/bin",
    "~/.cargo/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def augmented_path(path: str) -> str:
    parts = [p for p in path.split(os.pathsep) if p]
    for extra in _EXTRA_PATH_DIRS:
        full = os.path.expanduser(extra)
        if full not in parts and os.path.isdir(full):
            parts.append(full)
    return os.pathsep.join(parts)


__all__ = [
    "ASK_TIMEOUT",
    "PERMISSION_POLICIES",
    "AcpClient",
    "AgentInfo",
    "AgentSpec",
    "augmented_path",
    "contained_path",
    "decide_permission",
    "inherited_env",
    "normalize_update",
    "parse_initialize",
]
