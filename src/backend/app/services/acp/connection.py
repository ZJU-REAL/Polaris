"""ACP 的传输层：一个子进程 + 按行分隔的 JSON-RPC 2.0（#836）。

ACP（Agent Client Protocol，https://agentclientprotocol.com）里我们是 **client**：
把 Claude Code / Codex / Gemini CLI 这类 agent 当子进程拉起，经 stdin/stdout 说
JSON-RPC。和 MCP 不同，这条管道是**双向**的——agent 不只回我们的请求，还会反过来
向我们发请求（要权限、读写文件），并持续推 ``session/update`` 通知。

不引第三方 SDK：协议面很小（十来个方法），手写一层比拖进一个还在快速改版的包
更好掌控，尤其是取消、超时、进程退出这些边角。

## 为什么 stdout 的行上限要调大

asyncio 的 StreamReader 默认一行最多 64 KiB，超了直接抛错、整条连接报废。agent
写文件时会把整份内容放进一条消息里，几百 KiB 很常见，所以上限放到 32 MiB。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("polaris.acp")

#: 单行 JSON 上限（见模块头）
LINE_LIMIT = 32 * 1024 * 1024
#: stderr 只留最后这么多行，连不上时拿来解释原因
STDERR_TAIL = 40

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AcpError(RuntimeError):
    """ACP 连接/调用失败。``code`` 供上层分流展示（spawn-failed、timeout、rpc-error…）。"""

    def __init__(self, code: str, message: str, *, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(message)


class RpcMethodError(Exception):
    """入站请求的处理函数抛它，就按 JSON-RPC 错误回给 agent（而不是当成内部异常）。"""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


class AcpConnection:
    """一个 agent 子进程上的 JSON-RPC 连接。"""

    def __init__(
        self,
        command: str,
        args: list[str] | tuple[str, ...] = (),
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
    ) -> None:
        self.command = command
        self.args = list(args)
        self.env = env
        self.cwd = cwd
        self._on_notification = on_notification
        self._on_request = on_request
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._inbound: set[asyncio.Task[None]] = set()
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL)
        self._write_lock = asyncio.Lock()
        self._closed = False

    # ---------------------------------------------------------------- 生命周期

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closed

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    async def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
                limit=LINE_LIMIT,
                # 独立进程组：关连接时连同它拉起的孙进程（node、MCP 服务器）一起收掉
                start_new_session=os.name != "nt",
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            raise AcpError("spawn-failed", f"{self.command}: {exc}") from exc
        self._reader = asyncio.create_task(self._read_loop(), name=f"acp-read:{self.command}")
        self._stderr_reader = asyncio.create_task(
            self._read_stderr(), name=f"acp-stderr:{self.command}"
        )

    async def close(self, *, grace: float = 2.0) -> None:
        """关 stdin 让 agent 自己退出；等不到就 terminate，再等不到就 kill。"""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except TimeoutError:
                self._signal_group(proc, "terminate")
                try:
                    await asyncio.wait_for(proc.wait(), timeout=grace)
                except TimeoutError:
                    self._signal_group(proc, "kill")
                    with contextlib.suppress(Exception):
                        await proc.wait()
        for task in (self._reader, self._stderr_reader, *self._inbound):
            if task is not None and not task.done():
                task.cancel()
        self._fail_pending(AcpError("closed", "connection closed"))

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, how: str) -> None:
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if os.name != "nt":
                sig = signal.SIGTERM if how == "terminate" else signal.SIGKILL
                os.killpg(proc.pid, sig)
                return
            proc.terminate() if how == "terminate" else proc.kill()

    # ---------------------------------------------------------------- 发送

    async def request(self, method: str, params: dict[str, Any], *, timeout: float | None) -> Any:
        """发请求并等结果。``timeout=None`` 表示一直等（session/prompt 一轮可能跑很久）。"""
        if not self.alive:
            raise AcpError("not-running", self._exit_reason())
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as exc:
            raise AcpError("timeout", f"{method}: no response within {timeout:g}s") from exc
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.alive:
            await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                self._proc.stdin.write(line.encode("utf-8"))
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AcpError("not-running", self._exit_reason()) from exc

    # ---------------------------------------------------------------- 接收

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except ValueError:
                    # 超过 LINE_LIMIT：这条消息读不出来，连接的帧边界也就乱了
                    logger.warning("ACP 消息超过单行上限，断开：%s", self.command)
                    break
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    # 有的 agent 会往 stdout 打启动横幅；不是 JSON 的行当日志处理
                    self._stderr.append(f"[stdout] {text[:500]}")
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except asyncio.CancelledError:
            # 被取消（关连接、事件循环收尾）时不能再等进程退出：进程可能还活着，
            # 在 finally 里 await 它会让取消永远完不成——事件循环关闭时就卡死在这里
            self._fail_pending(AcpError("closed", "connection closed"))
            raise
        # stdout 关了 = 进程在退出；等它真退出好拿到退出码，但别无限等
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        self._fail_pending(AcpError("not-running", self._exit_reason()))

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        with contextlib.suppress(Exception):
            while True:
                raw = await stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr.append(line[:1000])

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        if method is None:
            # 我们发出的请求的回应
            fut = self._pending.get(msg_id) if isinstance(msg_id, int) else None
            if fut is None or fut.done():
                return
            if "error" in message:
                err = message.get("error") or {}
                fut.set_exception(
                    AcpError(
                        "rpc-error",
                        f"{err.get('message') or 'error'} (code {err.get('code')})"
                        + _error_detail(err.get("data")),
                        data=err.get("data"),
                    )
                )
            else:
                fut.set_result(message.get("result"))
            return
        params = message.get("params") or {}
        if msg_id is None:
            if self._on_notification is not None:
                self._spawn(self._on_notification(str(method), params))
            return
        self._spawn(self._answer(msg_id, str(method), params))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._inbound.add(task)
        task.add_done_callback(self._inbound.discard)

    async def _answer(self, msg_id: Any, method: str, params: dict[str, Any]) -> None:
        """回 agent 发来的请求。处理函数的异常一律折成 JSON-RPC 错误，绝不让读循环崩掉。"""
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if self._on_request is None:
            reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
        else:
            try:
                reply["result"] = await self._on_request(method, params)
            except RpcMethodError as exc:
                reply["error"] = {"code": exc.code, "message": str(exc)}
            except Exception as exc:  # noqa: BLE001 — agent 那边只需要知道失败了
                logger.warning("ACP 入站请求处理失败：%s", method, exc_info=True)
                reply["error"] = {"code": -32603, "message": f"internal error: {exc}"}
        with contextlib.suppress(AcpError):
            await self._send(reply)

    # ---------------------------------------------------------------- 杂项

    def _fail_pending(self, exc: AcpError) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)

    def _exit_reason(self) -> str:
        code = self._proc.returncode if self._proc is not None else None
        tail = self.stderr_tail.strip()
        head = f"agent exited (code {code})" if code is not None else "agent is not running"
        return f"{head}: {tail[-1500:]}" if tail else head


def _error_detail(data: Any) -> str:
    """错误的要紧信息常在 data 里（Codex 登录失效时 message 只有一句 Internal error，
    原因 "workspace routing discovery unauthorized (401)" 在 data.details 里）。"""
    if isinstance(data, dict):
        detail = data.get("details") or data.get("detail") or data.get("message")
        if detail:
            return f": {str(detail)[:500]}"
    elif isinstance(data, str) and data:
        return f": {data[:500]}"
    return ""


__all__ = ["AcpConnection", "AcpError", "RpcMethodError"]
