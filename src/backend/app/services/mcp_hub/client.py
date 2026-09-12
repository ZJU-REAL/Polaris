"""外部 MCP 服务器的客户端会话（对外调用方向）。

Polaris 此前只做 MCP **服务端**：把自己的检索工具暴露给别的 agent。这里补上
反方向——让 Polaris 去**调用**别人的 MCP 服务器，把专业软件（CAD、网格、求解器、
Blender 等）变成 agent 可调的工具。

## 为什么会话要常驻，而不是每次调用现连

专业软件的 MCP 服务器多半是**有状态**的：SolidWorks 里开着一个装配体、HyperMesh
里有一份正在划的网格。stdio 传输下「每次调用重连」等于每次重启那个进程，状态全丢，
整条 CAD→CAE 流水线根本走不下去。所以会话按服务器常驻，直到显式关闭。

## 为什么用一个专属 task 托住上下文

SDK 的传输与 ClientSession 都是异步上下文管理器，而 anyio 的 cancel scope 要求
**进入和退出在同一个 task**。如果在 A task 里 `__aenter__`、在 B task 里 `__aexit__`，
会抛 "Attempted to exit cancel scope in a different task"。所以上下文由一个专属
task 从头托到尾，其余 task 只借用已经建好的 session 发请求——ClientSession 内部
自带请求/响应多路复用，跨 task 并发调用是安全的。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("polaris.mcp_hub")

McpTransport = Literal["stdio", "http"]

# 外部软件启动可能很慢（要拉起 CAD 进程），但也不能无限等
DEFAULT_STARTUP_TIMEOUT = 60.0
DEFAULT_CALL_TIMEOUT = 300.0


class McpHubError(RuntimeError):
    """连接/调用外部 MCP 服务器失败。code 供上层分流展示。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(slots=True, frozen=True)
class McpServerSpec:
    """一台外部 MCP 服务器的连接方式。

    stdio：由我们把它作为子进程拉起（command/args/env/cwd）。
    http：连到已经在跑的服务（url），进程生命周期不归我们管。
    """

    id: str
    transport: McpTransport
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT

    def validate(self) -> None:
        if self.transport == "stdio" and not self.command:
            raise McpHubError("invalid-spec", f"{self.id}: stdio 传输必须给 command")
        if self.transport == "http" and not self.url:
            raise McpHubError("invalid-spec", f"{self.id}: http 传输必须给 url")


@dataclass(slots=True, frozen=True)
class McpToolRef:
    """外部服务器上的一个工具（已归一化成注册表认识的形状）。"""

    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any]


class McpSession:
    """一台外部 MCP 服务器的常驻会话。"""

    def __init__(self, spec: McpServerSpec) -> None:
        spec.validate()
        self.spec = spec
        self._session: Any = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._error: BaseException | None = None

    @property
    def connected(self) -> bool:
        return self._session is not None and self._error is None

    async def start(self) -> None:
        """拉起会话并完成 initialize 握手；失败时抛 McpHubError。"""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"mcp-session:{self.spec.id}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.spec.startup_timeout)
        except TimeoutError as exc:
            await self.close()
            raise McpHubError(
                "startup-timeout",
                f"{self.spec.id}: {self.spec.startup_timeout}s 内没有完成握手",
            ) from exc
        if self._error is not None:
            error = self._error
            await self.close()
            raise McpHubError("connect-failed", f"{self.spec.id}: {error}") from error

    async def _run(self) -> None:
        """上下文的唯一持有者：进入、置位就绪、等到关闭信号再退出（同一个 task）。"""
        try:
            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(await self._open_transport())
                # 两种传输返回的元组长度不同（http 多一个 get_session_id），只取前两个
                read, write = streams[0], streams[1]
                from mcp.client.session import ClientSession

                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._closing.wait()
        except Exception as exc:  # noqa: BLE001 — 任何失败都折叠成会话错误交给调用方
            self._error = exc
            logger.warning("MCP 会话失败：%s", self.spec.id, exc_info=True)
        finally:
            self._session = None
            self._ready.set()  # 叫醒还在等握手的 start()

    async def _open_transport(self) -> Any:
        if self.spec.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=self.spec.command or "",
                args=list(self.spec.args),
                env=dict(self.spec.env) or None,
                cwd=self.spec.cwd,
            )
            return stdio_client(params)

        from mcp.client.streamable_http import streamable_http_client

        return streamable_http_client(self.spec.url or "")

    def _require(self) -> Any:
        if self._session is None:
            raise McpHubError("not-connected", f"{self.spec.id}: 会话不可用")
        return self._session

    async def list_tools(self) -> list[McpToolRef]:
        session = self._require()
        result = await asyncio.wait_for(session.list_tools(), timeout=self.spec.call_timeout)
        refs: list[McpToolRef] = []
        for tool in result.tools:
            schema = getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}
            refs.append(
                McpToolRef(
                    server_id=self.spec.id,
                    name=tool.name,
                    description=(tool.description or "").strip(),
                    input_schema=dict(schema),
                )
            )
        return refs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用一个工具，把 MCP 结果归一化成 Polaris 工具的 dict payload。"""
        session = self._require()
        try:
            result = await asyncio.wait_for(
                session.call_tool(name, arguments), timeout=self.spec.call_timeout
            )
        except TimeoutError as exc:
            raise McpHubError("call-timeout", f"{self.spec.id}/{name}: 调用超时") from exc
        return normalize_call_result(result)

    async def close(self) -> None:
        self._closing.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — 关闭不再抛
                pass
        self._session = None


def normalize_call_result(result: Any) -> dict[str, Any]:
    """MCP 的 CallToolResult → Polaris 工具的 payload。

    结构化输出优先（structured_content 是服务端按 output_schema 给的机器可读结果），
    没有就把 content 里的文本块拼起来。``is_error`` 保留成数据而不是抛异常：工具
    自己报的失败是**结果**，agent 要能读到失败原因再决定下一步。
    """
    payload: dict[str, Any] = {}
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        payload.update(structured)

    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            texts.append(text)
    if texts and "text" not in payload:
        payload["text"] = "\n".join(texts)

    if getattr(result, "is_error", False):
        payload["is_error"] = True
    return payload


class McpHub:
    """按服务器 id 管理常驻会话。"""

    def __init__(self) -> None:
        self._sessions: dict[str, McpSession] = {}
        self._lock = asyncio.Lock()

    async def session(self, spec: McpServerSpec) -> McpSession:
        """取或建会话。同一 id 并发进来只连一次。"""
        async with self._lock:
            existing = self._sessions.get(spec.id)
            if existing is not None and existing.connected:
                return existing
            if existing is not None:
                await existing.close()
            session = McpSession(spec)
            await session.start()
            self._sessions[spec.id] = session
            return session

    def get(self, server_id: str) -> McpSession | None:
        return self._sessions.get(server_id)

    async def close(self, server_id: str) -> None:
        session = self._sessions.pop(server_id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        for server_id in list(self._sessions):
            await self.close(server_id)


_hub: McpHub | None = None


def get_mcp_hub() -> McpHub:
    global _hub
    if _hub is None:
        _hub = McpHub()
    return _hub


async def reset_mcp_hub() -> None:
    """测试与关机用：断开全部外部会话。"""
    global _hub
    if _hub is not None:
        await _hub.close_all()
    _hub = None
