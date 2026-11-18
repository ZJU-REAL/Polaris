"""同时在途的模型请求数：按网关的反应自己调（加性增、乘性减）。

各处扇出各有各的并发上限（一轮假设判定 6、想法写作 6、论文解读 8……），这些数是按
「接口受得了」定的。实际的网关常常受不了：开发用的 LiteLLM 网关按用户限并发，实测
同一时刻只放行三个请求，第四个起回 503「No available accounts」或 429「Concurrency
limit exceeded for user」，长流还会被中途掐断。每个被拒的请求各自退避重试，重试又撞
上同一个上限；一步里重试用完的那一个让整步失败、整步重来，已经付过钱的调用全部重付。

所以在 provider 这一层再加一道进程内的闸：同一个接口地址同时在途的请求不超过
``limit``。收到过载类的失败就把上限减半（不低于 ``floor``），连续成功若干次再加一。
上限只影响「同时发几个」，不改任何调用的内容，也不吞失败——失败照常往上抛，由原来
的重试逻辑处理，只是下一次不会再一起挤进去。

每个事件循环一份状态：asyncio 的同步原语绑定在创建它的循环上。
"""

import asyncio
import contextlib
import logging
import weakref
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

#: 起始上限：与各处扇出的上限同一量级（一轮假设判定、想法写作都是 6）。
START = 6
#: 下限：降到 1 会让「一个流在等另一个调用」的写法有机会卡死，2 留一点余地。
FLOOR = 2
CEILING = 16
#: 连续成功这么多次，上限加一。
RECOVER_AFTER = 8


class AdaptiveLimit:
    def __init__(
        self,
        start: int = START,
        *,
        floor: int = FLOOR,
        ceiling: int = CEILING,
        recover_after: int = RECOVER_AFTER,
    ) -> None:
        self.limit = max(floor, min(start, ceiling))
        self.floor = floor
        self.ceiling = ceiling
        self.recover_after = recover_after
        self.in_flight = 0
        self._streak = 0
        self._changed = asyncio.Condition()

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._changed:
            await self._changed.wait_for(lambda: self.in_flight < self.limit)
            self.in_flight += 1
        try:
            yield
        finally:
            async with self._changed:
                self.in_flight -= 1
                self._changed.notify_all()

    def overloaded(self) -> None:
        """接口说忙（限流、过载、流被掐断）：上限减半。"""
        self._streak = 0
        lowered = max(self.floor, self.limit // 2)
        if lowered < self.limit:
            logger.warning("model gateway is overloaded: at most %d requests in flight", lowered)
        self.limit = lowered

    async def succeeded(self) -> None:
        """一次成功；连续成功够数就把上限加一，叫醒排队的。"""
        self._streak += 1
        if self._streak >= self.recover_after and self.limit < self.ceiling:
            self._streak = 0
            self.limit += 1
            async with self._changed:
                self._changed.notify_all()


_limits: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AdaptiveLimit]]" = (
    weakref.WeakKeyDictionary()
)


def limit_for(key: str) -> AdaptiveLimit:
    """这个接口地址在当前事件循环里的那一道闸。"""
    loop = asyncio.get_running_loop()
    per_loop = _limits.setdefault(loop, {})
    if key not in per_loop:
        per_loop[key] = AdaptiveLimit()
    return per_loop[key]
