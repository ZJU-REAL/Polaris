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
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.acp.client import AcpClient, AgentSpec
from app.services.acp.connection import AcpError

logger = logging.getLogger("polaris.acp")

IDLE_TTL = 600.0
MAX_LIVE = 8
REAP_EVERY = 60.0


#: 后台跑着的收尾任务。只被事件循环弱引用的任务可能半路被回收，这里留强引用
_cleanups: set[asyncio.Future[Any]] = set()


async def run_cleanup(coro: Awaitable[None]) -> None:
    """把收尾放进独立任务、shield 着等它。

    调用方被取消时（断线；Starlette/anyio 的取消域会在之后**每一个** await 上反复投递
    取消）直接 await 收尾，收尾会在关进程、吊销令牌之前就被打断。放进独立任务后，
    取消照常往上抛，收尾在后台跑完。"""
    task = asyncio.ensure_future(coro)
    _cleanups.add(task)
    task.add_done_callback(_cleanups.discard)
    await asyncio.shield(task)


async def _close_and_unhook(client: AcpClient, hooks: list[Callable[[], Awaitable[None]]]) -> None:
    """关进程，然后跑收尾钩子——钩子不看关进程成没成功（令牌无论如何都要吊销）。"""
    try:
        with contextlib.suppress(Exception):
            await client.close()
    finally:
        for hook in hooks:
            with contextlib.suppress(Exception):
                await hook()


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
    #: 已经在会话上选过的模型（避免每轮重复 set_model）
    model: str = ""

    async def close(self) -> None:
        await run_cleanup(_close_and_unhook(self.client, self.on_close))


class AcpSessionPool:
    """对话 → 活会话。

    ``_guard`` 只护字典的增删（从不跨 await 拿着）：拉起进程、握手、开会话要好几秒
    到两分多钟，拿着全局锁做这些会把别的对话、关会话、改/删 agent、回收全都堵住。
    同一个对话的并发 open 由**每个 key 一把锁**串起来，不同对话互不等待。
    """

    def __init__(self, *, idle_ttl: float = IDLE_TTL, max_live: int = MAX_LIVE) -> None:
        self.idle_ttl = idle_ttl
        self.max_live = max_live
        self._live: dict[str, LiveSession] = {}
        self._guard = asyncio.Lock()
        #: 每个对话一把「正在开会话」的锁；没人拿着就随它被回收
        self._opening: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        #: agent → 配置代数。forget_agent 加一：开到一半的会话用的是旧配置，开完不入池
        self._generation: dict[uuid.UUID, int] = {}
        self._reaper: asyncio.Task[None] | None = None

    def get(self, key: str) -> LiveSession | None:
        live = self._live.get(key)
        if live is not None and not live.client.alive:
            return None
        return live

    def _key_lock(self, key: str) -> asyncio.Lock:
        lock = self._opening.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._opening[key] = lock
        return lock

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
        async with self._key_lock(key):
            async with self._guard:
                live = self._live.get(key)
                if live is not None and live.client.alive and live.agent_id == agent_id:
                    live.last_used = time.monotonic()
                    return live
                doomed: list[LiveSession] = []
                if live is not None:
                    # 进程没了，或这个对话换了 agent：旧会话作废
                    doomed.append(self._live.pop(key))
                doomed.extend(self._pick_victims())
                generation = self._generation.get(agent_id, 0)
            for victim in doomed:
                await victim.close()

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
                # 被取消时也得把进程关掉、令牌吊销掉（见 run_cleanup）
                await run_cleanup(_close_and_unhook(client, hooks))
                raise
            live = LiveSession(
                agent_id=agent_id,
                key=key,
                client=client,
                session_id=session_id,
                resumed=resumed,
                on_close=hooks,
            )
            # 同一个 key 的 open 被 key 锁串着，这里不会有别人插进来
            async with self._guard:
                stale = self._generation.get(agent_id, 0) != generation
                if not stale:
                    self._live[key] = live
            if stale:
                # 开的过程中 agent 被改/被删了：这个进程是按旧配置拉起的
                await live.close()
                raise AcpError("closed", "the agent was reconfigured; try again")
            self._ensure_reaper()
            return live

    async def close(self, key: str) -> None:
        async with self._guard:
            live = self._live.pop(key, None)
        if live is not None:
            await live.close()

    async def close_if(self, key: str, live: LiveSession) -> None:
        """关掉 ``live``；只有它还是 key 当前那个会话时才从池里摘掉。

        出错路径用它而不是 close(key)：出错到收尾之间，同一个对话可能已经换上了
        新会话（另一轮重新拉起的），按 key 关会把别人的新会话关掉。"""
        async with self._guard:
            if self._live.get(key) is live:
                self._live.pop(key, None)
        await live.close()

    async def forget_agent(self, agent_id: uuid.UUID) -> None:
        """agent 的配置改了或被删了：它名下所有活会话都得关掉。"""
        async with self._guard:
            self._generation[agent_id] = self._generation.get(agent_id, 0) + 1
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

    def _pick_victims(self) -> list[LiveSession]:
        """满了就按最久没用先腾位置（调用方拿着 _guard）。正在跑一轮的不动。

        几个对话同时在开会话时，入池那一刻可能短暂超出上限——宁可多一个进程，
        也不为了守上限把开会话串成一条队。"""
        idle = sorted(
            (v for v in self._live.values() if not v.lock.locked()), key=lambda v: v.last_used
        )
        victims: list[LiveSession] = []
        while len(self._live) >= self.max_live and idle:
            victim = idle.pop(0)
            self._live.pop(victim.key, None)
            victims.append(victim)
        return victims

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


__all__ = ["AcpSessionPool", "LiveSession", "get_pool", "run_cleanup", "shutdown_pool"]
