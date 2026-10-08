"""对话 ↔ 常驻 agent 进程（#836）。

为什么不每轮现拉一个进程：agent 的上下文（读过哪些文件、做到哪一步）在它自己的
会话里，进程一关就没了；而拉起一次 Claude Code 要好几秒。所以一个对话挂一个活
进程，空闲超时再收。进程没了（超时被收、服务重启）而 agent 支持 ``session/load``
时，用记下的会话号续上；不支持就开新会话，由上层把之前的对话摘要塞进提示词。

同一个对话同一时刻只跑一轮（每个会话一把锁）：ACP 一个会话里的 prompt 本来就是
串行的，并发发第二轮只会被 agent 拒掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.acp.client import AcpClient, AgentSpec

logger = logging.getLogger("polaris.acp")

IDLE_TTL = 600.0
MAX_LIVE = 8
REAP_EVERY = 60.0


@dataclass(slots=True)
class LiveSession:
    agent_id: uuid.UUID
    key: str
    client: AcpClient
    session_id: str
    #: 这个会话是续上来的（session/load），还是全新的
    resumed: bool = False
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: 关闭时要做的收尾（吊销给它的 MCP 令牌等）
    on_close: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()
        for hook in self.on_close:
            with contextlib.suppress(Exception):
                await hook()


class AcpSessionPool:
    def __init__(self, *, idle_ttl: float = IDLE_TTL, max_live: int = MAX_LIVE) -> None:
        self.idle_ttl = idle_ttl
        self.max_live = max_live
        self._live: dict[str, LiveSession] = {}
        self._guard = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    def get(self, key: str) -> LiveSession | None:
        live = self._live.get(key)
        if live is not None and not live.client.alive:
            return None
        return live

    async def open(
        self,
        key: str,
        *,
        agent_id: uuid.UUID,
        spec: AgentSpec,
        cwd: str,
        mcp_servers: Callable[
            [AcpClient], Awaitable[tuple[list[dict[str, Any]], list[Callable[[], Awaitable[None]]]]]
        ]
        | None = None,
        resume_session_id: str | None = None,
    ) -> LiveSession:
        """拿到 key 对应的活会话；没有就拉起进程、握手、开（或续）会话。"""
        async with self._guard:
            live = self._live.get(key)
            if live is not None and live.client.alive and live.agent_id == agent_id:
                live.last_used = time.monotonic()
                return live
            if live is not None:
                self._live.pop(key, None)
                await live.close()
            await self._evict_if_full()

            client = AcpClient(spec, root=cwd)
            hooks: list[Callable[[], Awaitable[None]]] = []
            try:
                info = await client.start()
                servers: list[dict[str, Any]] = []
                if mcp_servers is not None:
                    servers, hooks = await mcp_servers(client)
                session_id, resumed = None, False
                if resume_session_id and info.load_session:
                    try:
                        await client.load_session(resume_session_id, cwd, servers)
                        session_id, resumed = resume_session_id, True
                    except Exception:  # noqa: BLE001 — 续不上就开新的，别让整轮失败
                        logger.info("ACP session/load 失败，改开新会话", exc_info=True)
                if session_id is None:
                    session_id = await client.new_session(cwd, servers)
            except BaseException:
                await client.close()
                for hook in hooks:
                    with contextlib.suppress(Exception):
                        await hook()
                raise
            live = LiveSession(
                agent_id=agent_id,
                key=key,
                client=client,
                session_id=session_id,
                resumed=resumed,
                on_close=hooks,
            )
            self._live[key] = live
            self._ensure_reaper()
            return live

    async def close(self, key: str) -> None:
        async with self._guard:
            live = self._live.pop(key, None)
        if live is not None:
            await live.close()

    async def forget_agent(self, agent_id: uuid.UUID) -> None:
        """agent 的配置改了或被删了：它名下所有活会话都得关掉。"""
        async with self._guard:
            doomed = [k for k, v in self._live.items() if v.agent_id == agent_id]
            lives = [self._live.pop(k) for k in doomed]
        for live in lives:
            await live.close()

    async def close_all(self) -> None:
        async with self._guard:
            lives = list(self._live.values())
            self._live.clear()
        for live in lives:
            await live.close()
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None

    async def reap(self) -> int:
        """关掉空闲超时的、以及进程已经退出的会话。正在跑一轮的（锁被占着）不动。"""
        now = time.monotonic()
        async with self._guard:
            doomed = [
                k
                for k, v in self._live.items()
                if not v.lock.locked() and (not v.client.alive or now - v.last_used > self.idle_ttl)
            ]
            lives = [self._live.pop(k) for k in doomed]
        for live in lives:
            await live.close()
        return len(lives)

    async def _evict_if_full(self) -> None:
        idle = sorted(
            (v for v in self._live.values() if not v.lock.locked()), key=lambda v: v.last_used
        )
        while len(self._live) >= self.max_live and idle:
            victim = idle.pop(0)
            self._live.pop(victim.key, None)
            await victim.close()

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and not self._reaper.done():
            return

        async def _loop() -> None:
            while self._live:
                await asyncio.sleep(REAP_EVERY)
                with contextlib.suppress(Exception):
                    await self.reap()

        self._reaper = asyncio.create_task(_loop(), name="acp-pool-reaper")


_pool: AcpSessionPool | None = None


def get_pool() -> AcpSessionPool:
    global _pool
    if _pool is None:
        _pool = AcpSessionPool()
    return _pool


async def shutdown_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close_all()
        _pool = None


__all__ = ["AcpSessionPool", "LiveSession", "get_pool", "shutdown_pool"]
